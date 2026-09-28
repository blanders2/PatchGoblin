"""Stand-in for the claude/codex CLIs in tests: reads the prompt on stdin."""
import sys

mode = sys.argv[1]
prompt = sys.stdin.read()
print("working...", file=sys.stderr, flush=True)

if "FAIL" in prompt:
    print("simulated agent failure", file=sys.stderr)
    sys.exit(3)

if mode == "plan":
    print("1. Step one: create agent_output.txt\n2. Verify it exists")
else:
    with open("agent_output.txt", "w", encoding="utf-8") as fh:
        fh.write("written by fake agent\n")
    print("Created agent_output.txt")
