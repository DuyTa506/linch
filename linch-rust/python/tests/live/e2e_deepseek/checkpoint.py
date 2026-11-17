"""Print the checkpoint message of a post-compaction request body: checkpoint.py <body file>"""

import json
import re
import sys

body = json.load(open(sys.argv[1]))
for index, message in enumerate(body["messages"]):
    content = message["content"]
    text = content if isinstance(content, str) else "".join(b.get("text", "") for b in content)
    print(index, message["role"], len(text), "chars")
    if "<summary of earlier conversation>" in text:
        # Show the model's summary and the verbatim markers, with the long filler shortened.
        shortened = re.sub(r"((?:[a-z]+-\d\d ){6})(?:[a-z]+-\d\d ?)+", r"\1...", text)
        print(shortened[:3500])
