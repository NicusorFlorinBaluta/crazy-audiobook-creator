import json
from pathlib import Path

data = json.loads(Path("scratch/pr5_coderabbit_all_comments.json").read_text(encoding="utf-8"))

review_comments = data.get("review_comments", [])
issue_comments = data.get("issue_comments", [])

out = []
out.append(f"# Complete CodeRabbit Audit for PR #5 (Total Review Comments: {len(review_comments)}, Issue Comments: {len(issue_comments)})\n")

out.append("## Issue Comments / Overview\n")
for i, c in enumerate(issue_comments, 1):
    out.append(f"### Overview Comment #{i} by {c.get('user', {}).get('login')}")
    out.append("```markdown")
    out.append(c.get("body", ""))
    out.append("```\n")

out.append("## Detailed Review Comments\n")
for i, c in enumerate(review_comments, 1):
    path = c.get("path", "")
    line = c.get("line") or c.get("original_line") or c.get("position")
    body = c.get("body", "")
    commit_id = c.get("commit_id", "")[:7]
    comment_id = c.get("id")
    
    out.append(f"### [{i}/{len(review_comments)}] `{path}:{line}` (ID: {comment_id}, Commit: `{commit_id}`)")
    out.append("```markdown")
    out.append(body)
    out.append("```\n")
    out.append("---\n")

Path("scratch/all_coderabbit_summary.md").write_text("\n".join(out), encoding="utf-8")
print(f"Wrote all {len(review_comments)} review comments to scratch/all_coderabbit_summary.md")
