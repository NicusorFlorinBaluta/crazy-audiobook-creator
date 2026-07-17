"""LLM Script Director module.

Uses Ollama (Qwen3 32B) to analyze books and generate structured
audiobook scripts with character detection, voice descriptions,
and emotion tagging.
"""

from brain.director.ollama_client import OllamaClient
from brain.director.character_analyzer import CharacterAnalyzer
from brain.director.script_generator import ScriptGenerator

__all__ = ["OllamaClient", "CharacterAnalyzer", "ScriptGenerator"]
