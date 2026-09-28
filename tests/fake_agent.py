"""Stand-in for the claude/codex CLIs in tests: reads the prompt on stdin."""
import sys

mode = sys.argv[1]
prompt = sys.stdin.read()
print("working...", file=sys.stderr, flush=True)

if "FAIL" in prompt:
    print("simulated agent failure", file=sys.stderr)
    sys.exit(3)

if mode == "plan":
    if "Title: <" in prompt:
        print("Title: Create agent output file\n")
    print("1. Step one: create agent_output.txt\n2. Verify it exists")
    if "## Answers to your questions" in prompt:
        print("\nAnswers received: " + prompt.split("## Answers to your questions", 1)[1].split("\n\n")[0].strip())
        print("\n## Questions for you\nNone.")
    elif "ASK" in prompt:
        print("\n## Questions for you\n1. Which colour should the output be? [Red / Blue / Green]\n"
              "2. Should it log? [Yes / No]\n3. Any naming preferences?")
else:
    with open("agent_output.txt", "w", encoding="utf-8") as fh:
        fh.write("written by fake agent\n")
    print("Created agent_output.txt")
