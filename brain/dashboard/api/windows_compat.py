"""Windows-specific asyncio compatibility and resilience patches.

On Windows, Python's ProactorEventLoop uses IOCP (I/O Completion Ports)
and AcceptEx to accept incoming TCP connections. If a client connects and
drops/aborts immediately (e.g., mobile device roaming, Tailscale handshake reset,
network glitch), IOCP returns WinError 64 (ERROR_NETNAME_DELETED),
WinError 1236 (ERROR_CONNECTION_ABORTED), or WSAECONNRESET (10054).

By default, Python's `asyncio.proactor_events.BaseProactorEventLoop._start_serving`
treats any OSError during the `accept` callback as fatal to the server socket and calls
`sock.close()`, permanently closing the HTTP listener port (e.g. 8000) while the
rest of the process and pipeline keep running.

This module patches `_start_serving` and `IocpProactor.accept` to:
1. Catch transient client disconnect errors, log them, and keep the
   server socket listening.
2. Clean up any orphaned client socket without raising an unhandled task exception.
"""

from __future__ import annotations

import logging
import sys

logger = logging.getLogger(__name__)

# Transient client disconnect error codes on Windows during accept/handshake:
_CLIENT_DISCONNECT_ERRNOS = {
    64,  # ERROR_NETNAME_DELETED ("The specified network name is no longer available")
    121,  # ERROR_SEM_TIMEOUT ("The semaphore timeout period has expired")
    1236,  # ERROR_CONNECTION_ABORTED ("The network connection was aborted by the local system")
    10053,  # WSAECONNABORTED ("Software caused connection abort")
    10054,  # WSAECONNRESET ("Connection reset by peer")
    10060,  # WSAETIMEDOUT ("Connection timed out")
}


def patch_windows_proactor() -> None:
    """Patch asyncio's Windows Proactor event loop to survive client disconnects."""
    if sys.platform != "win32":
        return

    try:
        import asyncio.proactor_events
        import asyncio.windows_events
        from asyncio import exceptions, trsock
    except ImportError:
        return

    loop_cls = asyncio.proactor_events.BaseProactorEventLoop
    if getattr(loop_cls, "_windows_proactor_patched", False):
        return

    def _resilient_start_serving(
        self,
        protocol_factory,
        sock,
        sslcontext=None,
        server=None,
        backlog=100,
        ssl_handshake_timeout=None,
        ssl_shutdown_timeout=None,
    ):
        def loop(f=None):
            try:
                if f is not None:
                    try:
                        conn, addr = f.result()
                    except OSError as exc:
                        err_code = getattr(exc, "winerror", None) or exc.errno
                        if err_code in _CLIENT_DISCONNECT_ERRNOS:
                            logger.warning(
                                "Transient client disconnect during accept on socket %r (WinError %s); continuing listener loop",
                                sock,
                                err_code,
                            )
                            # Keep listener alive: schedule next accept unless loop or sock is closed
                            if not self.is_closed() and sock.fileno() != -1:
                                next_f = self._proactor.accept(sock)
                                self._accept_futures[sock.fileno()] = next_f
                                next_f.add_done_callback(loop)
                            return
                        # Real server socket error: re-raise to normal handler
                        raise

                    if self._debug:
                        logger.debug(
                            "%r got a new connection from %r: %r",
                            server,
                            addr,
                            conn,
                        )
                    protocol = protocol_factory()
                    if sslcontext is not None:
                        self._make_ssl_transport(
                            conn,
                            protocol,
                            sslcontext,
                            server_side=True,
                            extra={"peername": addr},
                            server=server,
                            ssl_handshake_timeout=ssl_handshake_timeout,
                            ssl_shutdown_timeout=ssl_shutdown_timeout,
                        )
                    else:
                        self._make_socket_transport(
                            conn,
                            protocol,
                            extra={"peername": addr},
                            server=server,
                        )
                if self.is_closed():
                    return
                f = self._proactor.accept(sock)
            except OSError as exc:
                if sock.fileno() != -1:
                    self.call_exception_handler(
                        {
                            "message": "Accept failed on a socket",
                            "exception": exc,
                            "socket": trsock.TransportSocket(sock),
                        }
                    )
                    sock.close()
                elif self._debug:
                    logger.debug("Accept failed on socket %r", sock, exc_info=True)
            except exceptions.CancelledError:
                sock.close()
            else:
                self._accept_futures[sock.fileno()] = f
                f.add_done_callback(loop)

        self.call_soon(loop)

    loop_cls._start_serving = _resilient_start_serving
    loop_cls._windows_proactor_patched = True

    # Also patch IocpProactor.accept to avoid unretrieved task exception and clean up conn
    proactor_cls = asyncio.windows_events.IocpProactor
    if not getattr(proactor_cls, "_windows_proactor_accept_patched", False):

        def _resilient_accept(self, listener):
            import _overlapped
            import socket
            import struct
            from asyncio import tasks

            self._register_with_iocp(listener)
            conn = self._get_accept_socket(listener.family)
            ov = _overlapped.Overlapped(0)
            ov.AcceptEx(listener.fileno(), conn.fileno())

            def finish_accept(trans, key, ov):
                ov.getresult()
                # Use SO_UPDATE_ACCEPT_CONTEXT so getsockname() etc work.
                buf = struct.pack("@P", listener.fileno())
                conn.setsockopt(
                    socket.SOL_SOCKET,
                    _overlapped.SO_UPDATE_ACCEPT_CONTEXT,
                    buf,
                )
                conn.settimeout(listener.gettimeout())
                return conn, conn.getpeername()

            async def accept_coro(future, conn):
                try:
                    await future
                except exceptions.CancelledError:
                    conn.close()
                    raise
                except OSError:
                    # Client disconnected before AcceptEx completed; close the ephemeral socket
                    try:
                        conn.close()
                    except Exception as e:
                        logger.debug("Failed to close socket on aborted AcceptEx: %s", e)

            future = self._register(ov, listener, finish_accept)
            coro = accept_coro(future, conn)
            tasks.ensure_future(coro, loop=self._loop)
            return future

        proactor_cls.accept = _resilient_accept
        proactor_cls._windows_proactor_accept_patched = True

    logger.info("Applied Windows asyncio Proactor accept resilience patch")
