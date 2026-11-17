import json
import sys

d = json.load(open(f"/home/aiqk3/Desktop/duy_dev/linch-agent-scratch/e2e/results/deepseek/{sys.argv[1]}/{sys.argv[2]}.json"))
for k, v in d["analysis"].items():
    if k != "requests":
        print(k, v)
