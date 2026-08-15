import urllib.request
import json
from pathlib import Path

headers = {'User-Agent': 'Python'}

def fetch_all_pages(base_url):
    all_results = []
    page = 1
    while True:
        url = f"{base_url}?per_page=100&page={page}"
        req = urllib.request.Request(url, headers=headers)
        with urllib.request.urlopen(req) as resp:
            data = json.loads(resp.read().decode('utf-8'))
            if not data:
                break
            all_results.extend(data)
            if len(data) < 100:
                break
            page += 1
    return all_results

comments = fetch_all_pages('https://api.github.com/repos/NicusorFlorinBaluta/crazy-audiobook-creator/pulls/5/comments')
issue_comments = fetch_all_pages('https://api.github.com/repos/NicusorFlorinBaluta/crazy-audiobook-creator/issues/5/comments')

output = {
    "review_comments": comments,
    "issue_comments": issue_comments
}

out_file = Path("scratch/pr5_coderabbit_all_comments.json")
out_file.write_text(json.dumps(output, indent=2), encoding="utf-8")
print(f"Successfully fetched ALL {len(comments)} review comments and {len(issue_comments)} issue comments across all pages into {out_file}.")
