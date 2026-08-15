import json
from pathlib import Path

data = json.loads(Path("scratch/pr5_coderabbit_comments.json").read_text(encoding="utf-8"))

comments = data.get("review_comments", [])

out = []
out.append(f"# CodeRabbit Review Comments Audit for PR #5 (Total: {len(comments)})\n")

for i, c in enumerate(comments, 1):
    path = c.get("path", "")
    line = c.get("line") or c.get("original_line") or c.get("position")
    body = c.get("body", "")
    commit_id = c.get("commit_id", "")[:7]
    comment_id = c.get("id")
    in_reply_to_id = c.get("in_reply_to_id")
    
    out.append(f"## Comment #{i} (ID: {comment_id}, Reply to: {in_reply_to_id})")
    out.append(f"**File:** `{path}` (Line: {line}) | **Commit:** `{commit_id}`")
    out.append("\n```markdown")
    out.append(body)
    out.append("```\n")
    out.append("---\n")

Path("scratch/pr5_coderabbit_summary.md").write_text("\n".join(out), encoding="utf-8")
print("Wrote all 30 comments into scratch/pr5_coderabbit_summary.md")
