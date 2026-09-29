"""Stand-in for the claude/codex/opencode/cline CLIs in tests: reads the prompt on stdin.
With --styled it first prints ANSI-styled thinking and tool-call chatter, like Cline does."""
import os
import sys

mode = sys.argv[1]
prompt = sys.stdin.read()
print("working...", file=sys.stderr, flush=True)
if os.environ.get("OPENCODE_CONFIG_CONTENT"):
    print("inline config: " + os.environ["OPENCODE_CONFIG_CONTENT"], file=sys.stderr, flush=True)

if mode == "plan" and "EDIT_DURING_PLAN" in prompt:
    with open("plan_edit.txt", "w", encoding="utf-8") as fh:
        fh.write("a planning agent should not write this\n")

if "FAIL" in prompt:
    print("simulated agent failure", file=sys.stderr)
    sys.exit(3)

if "--styled" in sys.argv:
    print("\x1b[2m[thinking] \x1b[0m\x1b[2mLet me look\x1b[0m\x1b[2m around.\x1b[0m")
    print("I'll read the files first.")
    print("\x1b[36m[read_files]\x1b[0m {\"files\": [\"a.txt\"]}\n   \x1b[90m> \x1b[0m\x1b[2m1 | hello\x1b[0m")
    sys.stdout.flush()

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
    if "## Feedback from reviewing the last AI run" in prompt:
        with open("followup.txt", "w", encoding="utf-8") as fh:
            fh.write("follow-up\n")
        print("Created followup.txt")
