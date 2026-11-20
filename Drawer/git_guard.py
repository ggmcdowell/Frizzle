"""The Bash refusal of a history rewrite in the product clone and the hub. Drawer/project-gate.py calls reason() for each Bash command that names git or
refs/original. It refuses:
  - git filter-branch and git filter-repo: filter-branch -f clears every ref under refs/original, the
    backups of other sessions too;
  - a delete of refs/original (update-ref -d, rm, trash, unlink, an xargs or update-ref pipe);
  - a force push (--force, -f, --force-with-lease, a +refspec) to main;
  - git reset --hard on main or another session's branch (session/<uuid8> not this one);
  - git branch -D (or -d --force) of a branch whose commits are not all on origin/main;
  - a HEAD move off a branch (head_move_reason): git checkout or switch that leaves the branch
    HEAD is on (a commit, another branch, -, --detach, -b, -c, --orphan) in a linked worktree or on main. A path
    checkout (-- <path>, -p, <tree-ish> <path>, a word that names no commit and is a path or no remote branch:
    git makes a local branch of a remote-only name), a move to the branch HEAD is on and any move
    of a detached HEAD (the repair) pass. A bare trailing -- marks the words before it as revisions, and the
    exact --force is not read as --force-create. Out of scope: git bisect start, a rebase stopped mid-run;
  - a git commit or push while core.hooksPath is unbound (hooks_reason): empty, or not the tracked Drawer/git-hooks or
    Drawer/product-hooks, in the hub, one of its worktrees or the product checkout (a worktree shares the setting).
    Any other repo passes;
  - a hook bypass: --no-verify (or git commit -n) on any git call, a core.hooksPath
    override (git -c, --config-env, a GIT_CONFIG_* variable), and a git config write or unset of
    core.hooksPath to any folder but the tracked Drawer/git-hooks or Drawer/product-hooks. A sh -c or
    bash -c body is judged the same way;
  - a delete or kill outside the session's own work (delete_reason): rm -r and trash of a
    folder that is not inside an own task worktree (a linked worktree whose folder name carries the session
    uuid8 as one of its - separated parts; never its root) or under a temp folder (/tmp, /var/tmp, $TMPDIR);
    a target the gate cannot place (a variable, a substitution, a brace, a pipe, xargs, a find -exec {}, or a ~
    word that is no home path: ~1, ~+1, a zsh hash -d name); a raw
    git worktree remove; git clean -x, -X or -d whose folder or pathspec is outside an own tree and the temp
    folders; git branch -D of a branch whose name carries no own id; pkill, killall, fuser -k, a kill of pid 0
    or a process group, and a kill by a computed pid (none left, a substitution, xargs, or a variable in a
    line that looks a process up); a line that does not parse and holds a delete or kill word. A file
    delete, a symlink delete and a kill by a literal pid, a job or $! pass. A heredoc fed to a shell is judged,
    and so is each echo or printf text in a line that pipes into a shell reading stdin. The gate
    fails closed on a line that runs text as code (feed_reason): with any shell or code runner word in
    it (bash5, csh, pwsh, eval, source, parallel, sudo -s, $SHELL, sed and awk), every text the line produces (echo,
    print, printf, yes, here-strings, process substitution bodies, documents that can reach a shell) is judged as
    one script in line order, escape-decoded and as written, wherever a pipe, group or redirect sends it; a -c body
    is judged before that text and with its positional words; $'...' text, a $( ) that only produces text and a
    variable set from plain text are written in; a stage that changes, cuts or reorders the text on its way (sed,
    tr, sort, grep, head, base64, $s) is refused, and so is code from a variable the line sets that the gate cannot
    resolve, in command position or in a line with a delete word.
    Command words match in any case (APFS runs RM). find -delete and find -exec rm -r or trash place the find
    roots and each literal operand of every -exec clause (-execdir: a relative one cannot be placed). env -C,
    env --chdir and sudo -D (bundled too: env -iC, sudo -nD) move the folder a relative target is placed from, only
    when env or sudo is a wrapper of the command, never an operand (rm -rf x env -C y); a cd or pushd to a ~ word that is
    no home path cannot be followed. A for header and command -v run nothing. A relative delete target is placed from every folder the line can be in (the event cwd and each cd,
    pushd target), since a ( ) subshell, a && branch or popd makes the last cd no proof. kill $$, kill $PPID and a
    kill of a variable set from a substitution in the line are refused; a variable set once from a plain mktemp
    is a temp folder;
  - a shell write to a law file (law_write_reason): AGENTS.md, SKILL.md, PRODUCT-CONVENTIONS.md,
    CLAUDE.md or CLAUDE.local.md as the target of a redirect, tee, sed -i or perl -i, cp, ln or install (into a
    folder too), any mv operand, or named by an inline python, node, perl or ruby script (-c, -e or a heredoc on
    stdin) that writes. An absolute or ~ path under ~/.claude passes, and so does one under a temp folder outside
    any checkout (no .git or hub marker above it); a relative path is fenced, since a cd can move it; the match is by file name, so a law-named file elsewhere is fenced. It skips doc-gate rules 2 and 4. Out of scope: git
    checkout, restore or mv of a law file, a name built at run time, a symlink the same line makes, a script file run by path, dd, rsync and truncate.
  Known limits (a Flight review; out of scope): rsync --delete, a script that deletes (python
  shutil.rmtree), trap bodies, a git -c alias, the wrappers script, watch, env -S, arch, exec -a and
  builtin, a shell fed by another producer (curl, cat of a file), a script file run by path,
  git update-ref -d or push --delete of
  another session's branch, and a mktemp variable changed by a command the line runs (eval, source).
A session's own reset and the delete of its merged, own-named work pass. It reads the branch with git, from the
event's cwd, each cd and each git -C in the command; it never writes.
"""
import fnmatch
import glob
import os
import re
import shlex
import subprocess

from gatelib import clip

SHARED = ("main",)
FILTER = re.compile(r"\bgit(?:\s+-\S+(?:\s+[^-\s;&|]\S*)?)*\s+filter-(?:branch|repo)\b|\bgit-filter-(?:branch|repo)\b")
ORIGINAL_DELETE = re.compile(r"\b(?:update-ref\b[^;&|\n]*\s(?:-d|--delete)\b|rm|trash|unlink|rmdir)\b[^;&|\n]*refs/original\b"
                             r"|refs/original\b[^;&\n]*\|\s*(?:\S+\s+)*(?:git\s+update-ref\b|xargs\b)")
SESSION_BRANCH = re.compile(r"session/([0-9a-f]{8})")
GIT_VALUE_OPTS = {"-C", "-c", "--git-dir", "--work-tree", "--namespace"}
PUSH_VALUE_OPTS = {"--repo", "-o", "--push-option", "--receive-pack", "--exec"}
REFUSED = "project-gate: history rewrite refused: "
HEAD_REFUSED = "project-gate: HEAD move refused: "
REMEDY = " Fix: reword with git commit --amend or a non-interactive rebase."
FILTER_REFUSED = REFUSED + "filter-branch and filter-repo clear refs/original, other sessions' backups." + REMEDY
ORIGINAL_REFUSED = REFUSED + "a delete of refs/original drops other sessions' backups." + REMEDY
HOOKS_KEY = "core.hookspath"
TRACKED_HOOKS = ("Drawer/git-hooks", "Drawer/product-hooks")
SHELLS = {"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "ash", "mksh", "yash", "posh", "rbash"}
SHELL_NAME = re.compile(r"(?:(?:ba|z|da|k|ok|lk|pdk|c|tc|mk|y|ya|po|rba|a|pw)?sh|fish|rc|es|xonsh|elvish|nu|ion|su|powershell)"
                        r"(?:-?\d[\w.]*)?(?:\.exe)?|osh|oil|ysh")
NON_POSIX = re.compile(r"(?:c|tc)sh|fish|pwsh|powershell|rc|es|nu|xonsh|elvish|ion")
CODE_RUNNERS = {"eval", "source", ".", "parallel", "at", "batch", "ssh", "script"}
TEXT_RUNNERS = {"sed", "gsed", "awk", "gawk", "mawk", "nawk"}  # sed e and awk system() run the text they read
PASS_STAGES = {"cat", "tee", "read"}  # they pass a produced text on whole
WRAPPERS = {"sudo", "time", "nohup", "command", "xargs", "exec", "timeout", "gtimeout", "nice", "caffeinate", "ionice",
            "busybox", "stdbuf", "unbuffer", "builtin", "arch", "setsid", "unshare", "strace", "doas", "xcrun", "repeat"}
PREFIX_WORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "}", "coproc", "noglob", "nocorrect", "-"} | WRAPPERS
WRAPPER_ARG = re.compile(r"-.*|\d+(?:\.\d+)?[smhd]?")  # an option, a count or a duration: timeout 60, nice -n 10
MESSAGE_VALUE_OPTS = {"-m", "--message", "-F", "--file", "-C", "-c", "--reuse-message", "--reedit-message",
                      "--fixup", "--squash", "--author", "--date", "--trailer", "--cleanup", "-o", "--push-option",
                      "--repo", "--receive-pack", "--exec", "-s", "--strategy", "-X", "--strategy-option"}
CONFIG_VALUE_OPTS = {"-f", "--file", "--blob", "--type", "--default", "--comment", "--value"}
CONFIG_READS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch", "-l", "--list"}
HOOK_REFUSED = "project-gate: hook bypass refused (SAF-7): "
NO_VERIFY_REFUSED = (HOOK_REFUSED + "--no-verify (or commit -n) skips the git hooks. Fix what the hook reports; to "
                     "gate uncommitted work, run the product gate with no commit.")
OVERRIDE_REFUSED = (HOOK_REFUSED + "a core.hooksPath override runs other hooks. Fix: plain git commit; "
                    "Drawer/product-hooks already runs the clone's own .githooks.")
CONFIG_REFUSED = (HOOK_REFUSED + "a git config write or unset of core.hooksPath. Fix: only Drawer/git-hooks or "
                  "Drawer/product-hooks.")
CONFORM_REFUSED = (HOOK_REFUSED + "HARNESS_CONFORM=off skips the commit gate like --no-verify. Fix what the gate "
                   "reports; never set HARNESS_CONFORM=off on a git command.")
CONFORM_OFF = re.compile(r"HARNESS_CONFORM=[\"']?off[\"']?", re.I)
CONFORM_TEXT = re.compile(r"(?i)\bHARNESS_CONFORM=[\"']?off\b[^;&|\n]*\bgit\b")
NO_VERIFY_TEXT = re.compile(r"\bgit\b[^;&|\n]*\s--no-veri")
HOOKS_TEXT = re.compile(r"(?i)\bgit\b[^;&|\n]*core\.hookspath")
ASSIGNMENT = re.compile(r"[A-Za-z_]\w*=.*", re.S)
WRAPPER_VALUES = {"xargs": {"-I", "-J", "-n", "-L", "-P", "-s", "-d", "-E", "-a", "-R", "-S"}, "timeout": {"-s", "-k"},
                  "gtimeout": {"-s", "-k"}, "nice": {"-n"}, "ionice": {"-c", "-n", "-p"},
                  "sudo": {"-u", "-g", "-C", "-D", "-h", "-p", "-r", "-t", "-U", "--chdir"}}  # the options that take a value
ENV_VALUE_OPTS = {"-u", "--unset", "-C", "--chdir", "-P"}
PARALLEL_VALUES = {"--colsep", "-j", "--jobs", "-P", "-a", "--arg-file", "-S", "--sshlogin", "-N", "-n", "--max-args",
                   "-d", "--delimiter", "-I", "--replace", "-E", "--joblog", "--results", "--tmpdir"}
DELETE_REFUSED = "project-gate: delete guard refused (SAF-4): "
DELETE_REMEDY = " Fix: own worktree or temp only; else NEED. Never reword."
WORKTREE_REFUSED = ("a raw git worktree remove skips the owner checks (SAF-5). Fix: <python> "
                    "Drawer/git-branch-policy.py worktree-remove --name <name>, from the main checkout.")
NEVER_KILL = "a kill by name, file or group reaches other sessions' processes. Fix: kill your own pid, by number."
COMPUTED_KILL = "its pid is computed in this line. Fix: kill your own process by a literal pid."
UNPARSED = "the line does not parse and holds a delete or kill word. Fix: close the quotes and retry."
DELETE_TEXT = re.compile(r"(?i)(?<![\w-])(?:rm|trash|kill|pkill|killall|fuser)(?![\w-])"
                         r"|\bgit\b[^;&|\n]*\s(?:clean|worktree|branch)(?![\w-])|\bfind\b[^;&|\n]*\s-delete\b")
LOOKUP = re.compile(r"(?<![\w-])(?:pgrep|pidof|ps|lsof|fuser|pstree)(?![\w-])")
SELF_KILL = "a kill of $$ or $PPID stops this shell or its session. Fix: kill a child by literal pid or $!."
MKTEMP = re.compile(r"(?<![\w$])([A-Za-z_]\w*)=[\"']?\$\(\s*mktemp\b([^)]*)\)")
UNPLACEABLE = re.compile(r"[$`{}]")
FIND_EXEC = {"-exec", "-execdir", "-ok", "-okdir"}
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_]\w*)\1[^\n]*\n(.*?)^\s*\2\s*$", re.S | re.M)


def unquoted_scan(command):
    """(the text with each comment cut and each unquoted newline a ; separator, the text with comments, single-quoted
    spans and escapes blanked). A # opens a
    comment only unquoted at a word start; bash runs a $( ) or a backtick pair only outside single
    quotes, so the second text is where substitutions are read."""
    text, live, quote, i, n = [], [], None, 0, len(command)
    while i < n:
        c = command[i]
        if quote != "'" and c == "\\" and i + 1 < n:
            joined = command[i + 1] == "\n"  # a backslash-newline joins the line
            escaped = command[i + 1] if quote == '"' and command[i + 1] in "$`" else None  # "\$x" is the text $x
            text.append("" if joined and quote != "'" else escaped or HELD.get(command[i + 1], command[i:i + 2]))  # r\<newline>m is rm
            live.append("  ")
            i += 2
            continue
        if quote is None and c == "\n":  # an unquoted newline ends a command; a quoted one stays in its word
            text.append(" ; ")
            live.append(c)
            i += 1
            continue
        if quote is None and c == "#" and (i == 0 or command[i - 1] in " \t\n;&|("):
            while i < n and command[i] != "\n":
                i += 1
            continue
        if quote is None and c == "&" and (command[i - 1:i] in ("<", ">") or command[i + 1:i + 2] == ">"):
            text.append(HELD["&"])  # 2>&1 and &>x are one redirect word, not a separator
            live.append(c)
            i += 1
            continue
        was = quote
        if quote is None and c in "'\"":
            quote = c
        elif quote == c:
            quote = None
        text.append(HELD.get(c, c) if was is not None and c != was else c)  # a quoted ( or ; is a word character
        live.append(" " if quote == "'" or c == "'" and quote is None else c)
        i += 1
    return "".join(text), "".join(live)


HELD = {c: chr(1 + k) for k, c in enumerate(";&|()")}  # a quoted or escaped separator, held out of shlex
RELEASE = str.maketrans({v: k for k, v in HELD.items()})


def split(command):
    """Each simple command as (the separator before it, its words); None when the text does not parse. Comments are
    cut first. The separator is '' for the first command."""
    lex = shlex.shlex(unquoted_scan(command)[0], posix=True, punctuation_chars=";&|()")
    lex.whitespace_split = True
    lex.commenters = ""
    found, words, sep = [], [], ""
    try:
        for token in lex:
            if token and set(token) <= set(";&|()"):
                if words:
                    found.append((sep, words))
                    sep = token
                else:
                    sep += token  # | ( keeps the pipe: echo x | (bash) feeds the subshell
                words = []
            else:
                words.append(token.translate(RELEASE))
    except ValueError:
        return None
    return found + [(sep, words)] if words else found


def segments(command):
    """Each simple command as its words; None when the text does not parse. Comments are cut first."""
    found = split(command)
    return None if found is None else [words for _, words in found]


ESCAPE = re.compile(r"\\(?:x([0-9a-fA-F]{1,2})|0?([0-7]{1,3})|u([0-9a-fA-F]{1,4})|U([0-9a-fA-F]{1,8})|([nrt])|(\\))")
FORMAT = re.compile(r"%[-#0 +]*\d*(?:\.\d+)?[a-zA-Z%]")
PIPE = re.compile(r"\)*\|&?;*\(*")  # a | or |&, after a ) group, before a newline (;) and a ( group
STDIN = {"-", "/dev/stdin", "/dev/fd/0"}


def stdin_path(word):
    """True for a path that can name stdin or another fd: -, /dev/stdin, /dev/fd/N, /proc/self/fd/N, and a /dev/
    path the gate cannot spell out (/dev/stdi?, /dev/std{in,}, /dev/fd/$((0)))."""
    return bool(re.fullmatch(r"-|/dev/stdin|/dev/fd/\d+|/proc/self/fd/\d+", word)) or (
        word.startswith("/dev/") and bool(re.search(r"[*?\[{$`]", word)))


ESCAPE_C = re.compile(r"\\(?:x([0-9a-fA-F]{1,2})|([0-7]{1,3})|u([0-9a-fA-F]{1,4})|U([0-9a-fA-F]{1,8})|([nrt])|(\\))")


def unescape(text, octal="echo"):
    """octal "echo": echo -e and printf %b read \\0nnn; octal "c": $'...' and a printf format read \\nnn (1 to 3 digits,
    a leading 0 counts), so \\0401 is a space and 1. text with each backslash escape decoded (n, r, t, xHH, octal, uHHHH), as echo -e and printf run them; a
    NUL is dropped, since a path cannot hold one."""
    def one(m):
        if m.group(6):
            return "\\"
        if m.group(5):
            return "\n" if m.group(5) == "n" else " "
        code = int(m.group(1) or m.group(3) or m.group(4), 16) if not m.group(2) else int(m.group(2), 8) & 0xFF  # \562 is r
        return chr(code) if 0 < code <= 0x10FFFF else ""
    return (ESCAPE_C if octal == "c" else ESCAPE).sub(one, text).replace("\0", "")


def echoed(words, decode=True):
    """The text a producer writes, escapes decoded, with the newline echo, print and yes add; None for another
    command. Producers: echo, print (zsh), printf (its format runs again for each set of leftover arguments), yes,
    and cat or tee of a here-string."""
    run, _ = strip(words)
    head = os.path.basename(run[0]).lower() if run else ""
    here = here_string(run)
    if head in ("cat", "tee") and here is not None:
        return here + "\n"
    args = [a for a in run[1:] if not re.fullmatch(r"\d*<<<", a)]  # '>/dev/null; rm -rf a' is text: keep it
    if here is not None and run.index(here) - 1 >= 1:
        args = [a for a in args if a != here]
    if head == "yes":
        return (" ".join(args) or "y") + "\n"
    if head == "print" and any(re.fullmatch(r"-\w+", a) and re.search(r"[^neErlR]", a[1:]) for a in args[:next(
            (k for k, a in enumerate(args) if not a.startswith("-") or a == "--"), len(args))]):
        return None  # print -u1, -P, -C1: a flag the gate does not model makes the text unreadable
    if head in ("echo", "print"):
        newline, raw, lines = True, False, False
        while args and re.fullmatch(r"-[neErlR]+", args[0]):
            newline = newline and "n" not in args[0]
            raw = raw or (head == "print" and "r" in args[0])
            lines = lines or (head == "print" and "l" in args[0])
            args = args[1:]
        args = args[1:] if head == "print" and args[:1] == ["--"] else args
        text = ("\n" if lines else " ").join(args)
        if raw or not decode:
            return text + ("\n" if newline else "")
        cut = text.split("\\c", 1)  # \c ends the output
        return unescape(cut[0]) + ("\n" if newline and len(cut) == 1 else "")
    if head != "printf":
        return None
    args = args[1:] if args[:1] == ["--"] else args
    if not args:
        return None
    fmt, values, out = args[0], args[1:], []
    slots = len([m for m in FORMAT.findall(fmt) if m != "%%"])
    if not decode:
        while True:
            chunk, values = values[:slots], values[slots:]
            given = iter(chunk)
            out.append(FORMAT.sub(lambda m: "%" if m.group(0) == "%%" else next(given, "")[:1 if m.group(0).endswith("c") else None], fmt))
            if not values or not slots:
                break
        return "".join(out)
    parts, convs = FORMAT.split(fmt), FORMAT.findall(fmt)  # the format's text decodes; a %s value stays as written
    while True:
        chunk, values = values[:slots], values[slots:]
        given = iter(chunk)
        piece = unescape(parts[0], "c")
        for conv, text in zip(convs, parts[1:]):
            value = "%" if conv == "%%" else next(given, "")
            value = value[:1] if conv.endswith("c") else value  # %c writes the first character
            piece += (unescape(value) if conv.endswith("b") else value) + unescape(text, "c")
        out.append(piece)
        if not values or not slots:
            break
    return "".join(out)


def here_string(run):
    """The here-string word of a command (bash <<< 'x', cat 0<<< 'x'), else None."""
    at = next((k for k, w in enumerate(run[:-1]) if re.fullmatch(r"\d*<<<", w)), None)
    return None if at is None else run[at + 1]


def is_shell(word):
    """True for a shell name (bash5, bash-5.2 and bash.exe too), or a command word the gate cannot name ($SHELL,
    "$SHELL", ${SHELL}, $l, /bin/$0): it fails closed and reads it as a shell."""
    return bool(SHELL_NAME.fullmatch(os.path.basename(word).lower())) or "$" in word or word.startswith("`")


def shell_run(words):
    """The words from the first shell or code runner word on (arch bash -c, flock f sh -c, env -S 'bash -s'), else []."""
    run, _ = strip(words)
    if run and ("$" in run[0] or run[0].startswith("`") or (re.search(r"[*?[]", run[0]) and run[0] not in ("[", "[["))
                or echoed(words) is not None):  # [ -z "$x" ] is the test builtin, not a glob command word
        return [] if echoed(words) is not None else run
    for k, w in enumerate(run):
        parts = w.split() if not k or run[k - 1] in ("-S", "--split-string") else [w]  # env -S 'bash -s'
        if any(SHELL_NAME.fullmatch(os.path.basename(p).lower()) or p.lower() in CODE_RUNNERS for p in parts):
            return parts + run[k + 1:]
    return []


def runs_code(words):
    """True when the command can run text as code: any shell or code runner word in it."""
    return bool(shell_run(words))


def stdin_shell(words):
    """True when the command can run shell text it reads from stdin: any shell or code runner word, or sudo -s."""
    lowered = [w.lower() for w in words]
    return runs_code(words) or "parallel" in lowered or ("sudo" in lowered and bool({"-s", "-i", "--shell", "--login"} & set(lowered)))


FILE_SINKS = {"tee", "sponge", "dd"}  # stages that write the text they read to a named file
GROUP_OPEN = {"{", "if", "case", "while", "until", "for", "select"}
GROUP_CLOSE = {"}", "fi", "esac", "done"}


def pipe_reach(found, k):
    """The stages after found[k] its output can reach: each stage of the pipeline it feeds, through a ( ), { } or
    loop group the pipe opens, up to the ; && || or & that ends that pipeline; a group that holds the producer
    passes its output on to what follows the group. None when the gate cannot place it: the producer sits in a
    substitution ($( ), <( ), backticks) or the groups do not balance."""
    def toks(sep):
        return [c for c in sep if c in "()|"] if PIPE.fullmatch(sep) else re.findall(r"\|\||&&|\|&|;;|[;&|()]", sep)

    def closers(words):
        n = 0
        while n < len(words) and words[n] in GROUP_CLOSE:
            n += 1
        return n

    stack = []  # the groups that hold the producer: "sub" for a substitution, "group" for ( ), { } or a loop
    for j, (sep, words) in enumerate(found[:k + 1]):
        if any("`" in w for w in words):
            return None
        prev = found[j - 1][1][-1] if j and found[j - 1][1] else ""
        for t in toks(sep) if j else []:
            if t == "(":
                stack.append("sub" if re.search(r"[$<>=]$", prev) else "group")  # T=$( , bash <( , a=(
            elif t == ")":
                if not stack:
                    return None
                stack.pop()
        n = closers(words)
        if n > len(stack):
            return None
        del stack[len(stack) - n:]
        stack += ["group"] * sum(w in GROUP_OPEN for w in words)
    if "sub" in stack:
        return None
    level, depth, ended, reach = len(stack), 0, False, []

    def leave():
        nonlocal depth, level, ended
        if depth:
            depth -= 1
            return True
        if not level:
            return False
        level, ended = level - 1, False  # the group that holds the producer ends: its output goes on
        return True

    for j in range(k + 1, len(found)):
        sep, words = found[j]
        if any("`" in w for w in words):
            return None
        for t in toks(sep):
            if t == "(":
                depth += 1
            elif t == ")":
                if not leave():
                    return None
            elif t not in ("|", "|&") and depth == 0:
                ended = True  # the producer's pipeline ends here
                if not level:
                    return reach
        for _ in range(closers(words)):
            if not leave():
                return None
        depth += sum(w in GROUP_OPEN for w in words)
        if not ended:
            reach.append(found[j])
    return reach


def reaches(found, k):
    """True when found[k]'s output can reach a shell, a code runner, a sed or awk that runs text, or a tee, sponge or
    dd that writes it to a file a later command can run (pipe_reach)."""
    later = pipe_reach(found, k)
    return later is None or any(stdin_shell(w) or stage_name(w) in TEXT_RUNNERS or (
        stage_name(w) in FILE_SINKS and len(drop_redirects(strip(w)[0])) > 1) for _, w in later)


def piped_texts(found, k):
    """The text of each producer before found[k] when a pipe runs before it: a shell that reads
    stdin runs what a pipe hands it, through any ( ) or { } group."""
    if not any(PIPE.fullmatch(sep) for sep, _ in found[1:k + 1]):
        return []
    return [t for t in (echoed(words) for _, words in found[:k]) if t]


def stage_name(words):
    """The command name of a stage, past redirects and the words that close a group or loop (done, fi, }, ))."""
    run = [w for w in drop_redirects(strip(words)[0]) if w.lower() not in ("done", "fi", "esac", "}", ")")]
    return os.path.basename(run[0]).lower() if run else ""


def ansi_c(command):
    """command with each unquoted $'...' (ANSI-C text) written as plain '...' text, its escapes decoded, and each
    $"..." as "..."; the gate reads $'\\x72m' as rm, as bash does."""
    out, quote, i, n, subs = [], None, 0, len(command), []  # subs: open "$( ... )" spans, each [paren depth]
    while i < n:
        c = command[i]
        if quote != "'" and c == "\\" and i + 1 < n:
            out.append(command[i:i + 2])
            i += 2
            continue
        if quote == '"' and command.startswith("$(", i):  # "$(echo $'cd a')": the substitution reads $'...' again
            subs.append([0])
            quote = None
            out.append("$(")
            i += 2
            continue
        if quote is None and subs and c in "()":
            subs[-1][0] += 1 if c == "(" else -1
            if subs[-1][0] < 0:
                subs.pop()
                quote = '"'
            out.append(c)
            i += 1
            continue
        if quote is None and c == "$" and command[i + 1:i + 2] == "'":
            j, body = i + 2, []
            while j < n and command[j] != "'":
                if command[j] == "\\" and j + 1 < n:
                    nxt = command[j + 1]
                    if re.match(r"(?:0+|x0+|u0+|U0+)(?![0-9a-fA-F])", command[j + 1:j + 10]):  # bash ends the text at a NUL
                        while j < n and command[j] != "'":
                            j += 2 if command[j] == "\\" else 1
                        break
                    if nxt == "c" and j + 2 < n:  # \cX is the control character of X: \cj is a newline
                        body.append(chr(ord(command[j + 2]) & 31))
                        j += 3
                        continue
                    body.append("\\\\" if nxt == "\\" else nxt if nxt in "'\"?" else "\x1b" if nxt in "eE" else command[j:j + 2])
                    j += 2
                    continue
                body.append(command[j])
                j += 1
            text = unescape("".join(body), "c") if j < n else None
            if text is None:  # no closing quote: leave it for the parse to refuse
                out.append(command[i:])
                break
            out.append("'" + text.replace("'", "'\"'\"'") + "'")
            i = j + 1
            continue
        if quote is None and command.startswith("<<<", i):  # bash<<<'x': the here-string is its own word
            out.append(("" if not out or out[-1][-1:].isspace() or out[-1][-1:].isdigit() else " ") + "<<< ")
            i += 3
            continue
        if quote is None and c == "$" and command[i + 1:i + 2] == '"':
            i += 1
            continue
        if quote is None and c in "'\"":
            quote = c
        elif quote == c:
            quote = None
        out.append(c)
        i += 1
    return "".join(out)


def quoted_mask(command):
    """For each character of command, True when it lies inside quotes, after a backslash or in a # comment."""
    mask, quote, i = [False] * len(command), None, 0
    while i < len(command):
        c = command[i]
        if quote is None and c == "#" and (i == 0 or command[i - 1] in " \t\n;&|("):
            end = command.find("\n", i)
            end = len(command) if end < 0 else end
            mask[i:end] = [True] * (end - i)
            i = end
            continue
        if quote != "'" and c == "\\":
            mask[i:i + 2] = [True] * len(mask[i:i + 2])
            i += 2
            continue
        if quote is None and c in "'\"":
            quote = c
        elif quote == c:
            quote = None
        mask[i] = quote is not None or c in "'\""
        i += 1
    return mask


def documents(raw):
    """Each real heredoc in raw (match groups: quote, word, body): an opener in quotes, after a backslash, in a
    comment or a <<< here-string is text, not a document (echo '<<EOF' hid the next lines)."""
    mask, pos, found = quoted_mask(raw or ""), 0, []
    while True:
        m = HEREDOC.search(raw or "", pos)
        if not m:
            return found
        head = raw[:m.start()]
        inside = head.count("((") > head.count("))") or head.count("${") > head.count("}")  # $((1<<N)), ${x:-<<a}
        if mask[m.start()] or inside or raw[m.start() - 1:m.start()] == "<" or raw[m.start() + 2:m.start() + 3] == "<":
            pos = m.start() + 2
            continue
        found.append(m)
        pos = m.end()


def word_forms(command):
    """command with $IFS and ${IFS} written as a space and each unquoted brace list written out ({rm,-rf,a} as
    rm -rf a, {sh,} as sh), as bash splits and expands them."""
    command = re.sub(r"\$(?:IFS\b|\{IFS\})", " ", command)
    for _ in range(3):  # ${x:-rm -rf a}: an unset name runs its default text
        command = re.sub(r"\$\{[A-Za-z_]\w*:?[-=+]((?:[^{}]|\{[^{}]*\})*)\}", lambda m: m.group(1), command)
    for _ in range(4):
        found = brace_list(command)
        if not found:
            break
        start, end, alts = found
        left = re.search(r"[^\s;&|(){}'\"]*\Z", command[:start]).group(0)
        right = re.match(r"[^\s;&|(){}'\"]*", command[end:]).group(0)
        words = [left + alt + right for alt in alts]
        command = command[:start - len(left)] + " ".join(w for w in words if w) + command[end + len(right):]
    return command


def brace_list(command):
    """(start, end, alternatives) of the first unquoted {a,b} list (quoted parts allowed: {echo,'rm -rf a'}), else
    None."""
    mask = quoted_mask(command)
    for i, c in enumerate(command):
        if c != "{" or mask[i] or (i and command[i - 1] == "$"):
            continue
        commas, j = [], i + 1
        while j < len(command) and not (command[j] in "{}" and not mask[j]) and not (command[j].isspace() and not mask[j]):
            if command[j] == "," and not mask[j]:
                commas.append(j)
            j += 1
        if commas and j < len(command) and command[j] == "}" and not mask[j]:
            cuts = [i] + commas + [j]
            return i, j + 1, [command[a + 1:b] for a, b in zip(cuts, cuts[1:])]
    return None


def spans(live, opener):
    """(start, end) of each opener ... ) span in the live text, ( ) nesting matched; end is past the )."""
    found = []
    for m in re.finditer(re.escape(opener), live):
        depth, j = 1, m.end()
        while j < len(live) and depth:
            depth += {"(": 1, ")": -1}.get(live[j], 0)
            j += 1
        found.append((m.start(), j))
    return found


def script_of(body, decode=True):
    """(the text a command text writes, the stage that changes it): text None when no producer the gate reads
    writes it (direnv hook zsh: out of scope), stage None when every other stage passes the text on whole."""
    found = split(expand(body, decode)[0]) or []
    texts = [echoed(words, decode) for _, words in found]
    if not any(texts):
        return None, here_filter(found)
    bad = next((stage_name(w) for (_, w), t in zip(found, texts) if t is None and stage_name(w) not in PASS_STAGES),
               None)
    return (None, bad) if bad else ("".join(t for t in texts if t), None)


def here_filter(found):
    """The first command that changes a here-string's text (sed 's/X/m/' <<< 'rX'), else None."""
    return next((stage_name(w) for _, w in found if here_string(strip(w)[0]) is not None and echoed(w) is None
                 and stage_name(w) not in PASS_STAGES and not runs_code(w)), None)


def variables(found):
    """name -> text for each variable the line sets from plain text: x='rm -rf a', export x=..., read x <<< '...',
    and a read in a loop a pipe feeds."""
    out, events = {}, {}
    for name in set_events(found):
        events[name] = events.get(name, 0) + 1
    reads = 0
    read_words = [w for _, w in found if strip(w)[0][:1] == ["read"]]
    loop_reads = len(read_words) if any(w[0] in ("while", "until") for w in read_words) else 0
    for k, (sep, words) in enumerate(found):
        lead = words[1:] if words[:1] and words[0] in ("export", "local", "declare", "typeset", "readonly") else words
        if sep in ("", ";") and lead and all(ASSIGNMENT.fullmatch(w) and "+=" not in w.split("=", 1)[0] + "=" for w in lead):
            out.update(p for p in (w.split("=", 1) for w in lead)  # plain text, set once, outside && || ( ): a sure value
                       if not re.search(r"[$`]", p[1]) and events.get(p[0]) == 1)
        run, _ = strip(words)
        if run[:1] == ["read"]:
            here = here_string(run)
            value = here if here is not None else "".join(piped_texts(found, k)) or "".join(  # { read x; } <<< '...'
                h + "\n" for h in (here_string(strip(w)[0]) for _, w in found) if h is not None) or "".join(
                t for t in (echoed(w) for _, w in found[k + 1:]) if t) or None  # done < <(echo '...')
            if loop_reads > 1:  # while read a; do read b; ...: the lines the reads take are not followed
                value = None
            elif here is None and value is not None and words[0] not in ("while", "until"):  # read a; read b: next line
                lines = value.split("\n")
                value = lines[reads] if reads < len(lines) else ""
                reads += 1
            names = [w for w in run[1:] if re.fullmatch(r"[A-Za-z_]\w*", w)] if value is not None else []
            fields = value.strip("\n").split() if value is not None else []
            for i, name in enumerate(names):  # read a b <<< 'cd x': a is cd, b is x
                if events.get(name) == 1:
                    out[name] = " ".join(fields[i:]) if i == len(names) - 1 else (fields[i] if i < len(fields) else "")
    return out


def expand_once(command, decode=True):
    """(command with each $( ) or backtick pair whose body only produces text written as that text, and each $x
    the line sets from plain text written as its value; the stage that changes a produced text, else None)."""
    live = unquoted_scan(command)[1]
    if len(live) != len(command):
        return command, None
    found = [(s, e, command[s + 2:e - 1]) for s, e in spans(live, "$(")]
    found += [(m.start(), m.end(), command[m.start() + 1:m.end() - 1]) for m in re.finditer(r"`[^`]*`", live)]
    bad = None
    for s, e, body in sorted(found, reverse=True):
        if any(s2 < s and e <= e2 for s2, e2, _ in found):
            continue  # a nested pair is written by its outer pair
        text, changed = script_of(body, decode)
        bad = bad or changed
        if text is not None:
            text = text.rstrip("\n")
            quoted = re.search(r"(?:=|<<<\s*)$", command[:s])  # x=$(...) and <<< $(...) keep the text one word
            command = command[:s] + (shlex.quote(text) if quoted else text) + command[e:]
    names = variables(split(command) or [])
    live = unquoted_scan(command)[1]
    if names and len(live) == len(command):
        for m in reversed(list(re.finditer(r"\$(?:\{([A-Za-z_]\w*)\}|([A-Za-z_]\w*))", live))):
            name = m.group(1) or m.group(2)
            if name in names:
                command = command[:m.start()] + names[name] + command[m.end():]
    return command, bad


def expand(command, decode=True):
    """(command with produced text and plain variables written in, as bash runs them: $(echo rm) -rf x, eval
    "$(printf 'rm -rf x')", x='rm -rf a'; eval "$x"; the stage that changes a produced text)."""
    bad = None
    for _ in range(4):
        new, changed = expand_once(command, decode)
        bad = bad or changed
        if new == command:
            break
        command = new
    return command, bad


def resolve(base, path):
    return os.path.normpath(os.path.join(base, os.path.expanduser(path)))


def git_call(words, cwd):
    """(folder, subcommand, args) of one git call, else None."""
    i = 0
    while i < len(words) and re.fullmatch(r"[A-Za-z_]\w*=.*", words[i]):
        i += 1
    if i >= len(words) or os.path.basename(words[i]).lower() != "git":
        return None
    i += 1
    folder = cwd
    while i < len(words) and words[i].startswith("-"):
        if words[i] in GIT_VALUE_OPTS and i + 1 < len(words):
            folder = resolve(folder, words[i + 1]) if words[i] == "-C" else folder
            i += 2
        else:
            i += 1
    return (folder, words[i], words[i + 1:]) if i < len(words) else None


def git(folder, *args):
    """Stdout of a read-only git call, or None when it fails."""
    try:
        run = subprocess.run(["git", "-C", folder, *args], capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    return run.stdout.strip() if run.returncode == 0 else None


def branch(folder):
    return git(folder, "symbolic-ref", "--short", "-q", "HEAD")


def flags(args):
    """The one-letter flags of each short cluster (-Df gives D and f) and every long option."""
    found = set()
    for a in args:
        if a.startswith("--"):
            found.add(a.split("=", 1)[0])
        elif a.startswith("-") and len(a) > 1:
            found |= set(a[1:])
    return found


def push_reason(folder, args):
    given = flags(args)
    force = bool(given & {"f", "--force", "--force-with-lease"})
    positional, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in PUSH_VALUE_OPTS:
            skip = True
        elif not a.startswith("-"):
            positional.append(a)
    specs = positional[1:]
    targets = [(s.startswith("+"), s.lstrip("+").split(":")[-1]) for s in specs] or [(False, "HEAD")]
    for plus, target in targets:
        if not (force or plus):
            continue
        name = branch(folder) if target == "HEAD" else re.sub(r"^refs/heads/", "", target)
        if name is None or name in SHARED:
            return (f"force push to {clip(name or 'an unread branch')} rewrites shared history. Fix: rebase on "
                    f"origin/main, then git push origin HEAD:main.")
    return None


def reset_reason(folder, args, own):
    if "--hard" not in args:
        return None
    name = branch(folder)
    other = SESSION_BRANCH.fullmatch(name or "")
    if name in SHARED or (other and other.group(1) != own):
        return (f"reset --hard on {clip(name)} drops shared or another session's work. Fix: reset only your own "
                f"branch, or revert.")
    return None


def branch_reason(folder, args, own):
    given = flags(args)
    if not ("D" in given or (given & {"d", "--delete"} and given & {"f", "--force"})):
        return None
    names = [a for a in args if not a.startswith("-")]
    for name in names:
        ahead = git(folder, "rev-list", "--count", f"origin/main..refs/heads/{name}")
        if ahead != "0":
            return REFUSED + (f"git branch -D {clip(name)}: {clip(ahead or 'unread', 8)} commits not on origin/main. "
                              f"Fix: land them, or use git branch -d.")
    for name in names:
        if not own or own not in re.split(r"[-/]", name.lower()):
            return DELETE_REFUSED + (f"git branch -D {clip(name)}: the name carries no id of this session (SAF-5). "
                                     f"Fix: git branch -d, or leave it to its owner.")
    return None


def linked(folder):
    """True when folder is in a linked worktree: its git dir is not the common git dir."""
    dirs = git(folder, "rev-parse", "--absolute-git-dir", "--git-common-dir")
    if not dirs or len(dirs.splitlines()) != 2:
        return False
    own, common = dirs.splitlines()
    return os.path.realpath(own) != os.path.realpath(os.path.join(folder, common))


def long_opt(a, names):
    """True when the long option a (an unambiguous prefix too, as git takes it) is one of names."""
    a = a.split("=", 1)[0]
    return len(a) > 3 and any(n.startswith(a) for n in names)


def head_move_reason(folder, sub, args):
    """Why a git checkout or switch is refused, else None: it moves HEAD off the branch it is on, in a linked
    worktree or on a shared branch. A path checkout, a move to the branch HEAD is on and any
    move of a HEAD that is already detached (the repair) pass. A word that names no commit is a path only when a
    path of that name exists or no remote branch has that name: else git makes a local branch of it (remote DWIM). A bare trailing -- marks the words before it as revisions, so no path pass applies."""
    name = branch(folder)
    if name is None or (name not in SHARED and not linked(folder)):
        return None
    revs_only = "--" in args
    if revs_only:
        k = args.index("--")
        if k + 1 < len(args):
            return None  # git checkout [<tree-ish>] -- <path>: a path checkout
        args = args[:k]
    if any(a in ("-p", "--patch") or long_opt(a, ("--pathspec-from-file",)) for a in args):
        return None
    short = {"b", "B"} if sub == "checkout" else {"d", "c", "C"}
    longs = ("--detach", "--orphan") + (("--create", "--force-create") if sub == "switch" else ())
    moves = any(long_opt(a, longs) for a in args if a.startswith("--") and a.split("=", 1)[0] != "--force") or bool(flags(
        [a for a in args if not a.startswith("--")]) & short)
    words, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a in ("--conflict",):
            skip = True
        elif a == "-" or not a.startswith("-"):
            words.append(a)
    target = words[0] if words else None
    if not moves and sub == "checkout" and len(words) > 1:
        return None  # git checkout <tree-ish> <path>: a path checkout
    if not moves and target in (None, name, "HEAD", "@"):
        return None
    if not moves and sub == "checkout" and not revs_only and target != "-" and not target.startswith("@{-") and git(
            folder, "rev-parse", "--verify", "-q", target + "^{commit}") is None and (
            os.path.lexists(os.path.join(folder, target)) or target not in set(
                (git(folder, "for-each-ref", "--format=%(refname:lstrip=3)", "refs/remotes") or "").splitlines())):
        return None  # git checkout <path>
    return (f"git {sub} leaves branch {clip(name)}; hub-land then says 'has no worktree'. Fix: read in place with "
            f"git show <rev>:<path> or git diff <rev>.")


def options(args):
    """The option words of a git subcommand: a value after a value option and every word after -- are not options,
    so a message such as -m "-n" stays a message."""
    found, skip = [], False
    for a in args:
        if skip:
            skip = False
        elif a == "--":
            break
        elif a.startswith("-"):
            found.append(a)
            skip = a in MESSAGE_VALUE_OPTS or (len(a) > 2 and not a.startswith("--") and a[-1] in "mFCc")
        # a positional word is no option
    return found


def no_verify(sub, args):
    """True when the args skip the hooks: --no-verify or an abbreviation git accepts, or commit -n."""
    opts = options(args)
    for a in opts:
        name = a.split("=", 1)[0]
        if len(name) >= len("--no-veri") and "--no-verify".startswith(name):
            return True
    return sub == "commit" and any("n" in a[1:] for a in opts if not a.startswith("--"))


def tracked_hooks(value):
    """True when a hooks path is the tracked folder: Drawer/git-hooks or Drawer/product-hooks, relative, or under a
    hub root that holds harness.toml."""
    path = os.path.normpath(os.path.expanduser(value)).replace("\\", "/")
    if path in TRACKED_HOOKS:
        return True
    return (os.path.isabs(path) and path.endswith(tuple("/" + t for t in TRACKED_HOOKS))
            and os.path.isfile(os.path.join(os.path.dirname(os.path.dirname(path)), "harness.toml")))


HOOKS_UNBOUND = ("project-gate: core.hooksPath is unbound; this git %s skips the secret scan and name check. "
                 "Fix: <python> Drawer/git-branch-policy.py install --repo %s")


def hub_repo(top, common):
    """True when the repo at top is the hub (its harness.toml names a marker file that exists), a worktree of it, or the
    product checkout ([product] path, or its task override): the repos whose commits the hooks gate."""
    import tomllib
    try:
        with open(os.path.join(top, "harness.toml"), "rb") as fh:
            marker = tomllib.load(fh).get("project", {}).get("marker_file")
        if marker and os.path.isfile(os.path.join(top, marker)):
            return True
    except (OSError, ValueError):
        pass
    try:
        import harness_config
        _, value = harness_config.product_path(harness_config.load(harness_config.hub_root(os.path.dirname(__file__))))
    except (SystemExit, OSError, ValueError, KeyError):
        return False
    theirs = git(os.path.expanduser(value), "rev-parse", "--path-format=absolute", "--git-common-dir") if value else None
    return bool(theirs) and os.path.realpath(theirs) == os.path.realpath(common)


def hooks_reason(folder, sub):
    """The refusal of a git commit or push in the hub, a hub worktree or the product checkout while core.hooksPath is
    empty or not the tracked hooks folder, else None (every other repo passes)."""
    found = git(folder, "rev-parse", "--show-toplevel", "--path-format=absolute", "--git-common-dir")
    if not found or len(found.splitlines()) != 2:
        return None
    top, common = found.splitlines()
    if not hub_repo(top, common) or tracked_hooks(git(folder, "config", "--get", "core.hooksPath") or ""):
        return None
    return HOOKS_UNBOUND % (sub, clip(top, 40))


def config_reason(args):
    """The refusal of a git config write or unset of core.hooksPath, else None. A read passes."""
    given, pos, skip = set(), [], False
    for a in args:
        if skip:
            skip = False
        elif a.startswith("-"):
            given.add(a.split("=", 1)[0])
            skip = a in CONFIG_VALUE_OPTS
        else:
            pos.append(a)
    verb = pos.pop(0) if pos and pos[0] in ("get", "list", "set", "unset") else ""
    if not pos or pos[0].lower() != HOOKS_KEY or verb in ("get", "list") or given & CONFIG_READS:
        return None
    if verb == "unset" or given & {"--unset", "--unset-all"} or given & {"--add", "--replace-all"} and len(pos) < 2:
        return CONFIG_REFUSED
    if len(pos) < 2:
        return None
    return None if tracked_hooks(pos[1]) else CONFIG_REFUSED


def hook_bypass_reason(words):
    """The refusal of a hook bypass in one simple command, else None. It judges words, so a quoted message or a
    grep pattern that names --no-verify or core.hooksPath passes."""
    i = 0
    env = []
    while i < len(words) and ASSIGNMENT.fullmatch(words[i]):
        env.append(words[i])
        i += 1
    wrapped = False
    while i < len(words) and (words[i] in PREFIX_WORDS or (wrapped and WRAPPER_ARG.fullmatch(words[i]))):
        wrapped = wrapped or words[i] in WRAPPERS
        i += 1  # if, then, !, {, sudo -E, timeout 60, nice -n 10, xargs -n1 ...: the command they run is judged
    head = os.path.basename(words[i]).lower() if i < len(words) else ""
    if head in ("export", "env"):
        env += [w for w in words[i + 1:] if ASSIGNMENT.fullmatch(w)]
    if any(w.startswith("GIT_CONFIG") and HOOKS_KEY in w.lower() for w in env):
        return OVERRIDE_REFUSED
    conform_off = any(CONFORM_OFF.fullmatch(w) for w in env)
    if conform_off and head in ("git", "export"):  # HARNESS_CONFORM=off git commit; export HARNESS_CONFORM=off
        return CONFORM_REFUSED
    if head == "env":  # env A=b git ...: judge the command env runs
        rest = words[i + 1:]
        while rest and (ASSIGNMENT.fullmatch(rest[0]) or rest[0].startswith("-")):
            rest = rest[1:]
        if conform_off and rest and os.path.basename(rest[0]).lower() == "git":
            return CONFORM_REFUSED
        return hook_bypass_reason(rest) if rest else None
    if head != "git":
        return None
    i += 1
    while i < len(words) and words[i].startswith("-"):
        value = words[i + 1] if words[i] in GIT_VALUE_OPTS | {"--config-env"} and i + 1 < len(words) else ""
        if HOOKS_KEY in (words[i] + " " + value).lower():
            return OVERRIDE_REFUSED
        i += 2 if value else 1
    if i >= len(words):
        return None
    sub, args = words[i], words[i + 1:]
    if no_verify(sub, args):
        return NO_VERIFY_REFUSED
    return config_reason(args) if sub == "config" else None


def strip(words, moved=None):
    """(the command words after VAR=value words, if/then words and wrappers such as sudo, xargs, timeout and env;
    True when xargs feeds the command its arguments). Command words match in any case: APFS runs RM as rm. moved, a
    list, gets each folder an env -C or sudo -D in the command's own wrappers moves it to, in order."""
    i, fed, wrapper = 0, False, None
    if words[:1] == ["function"] and len(words) > 1:  # function f { rm -rf a; }: the body runs when f is called
        words = words[2:]
        words = words[1:] if words[:1] in (["{"], ["()"]) else words
    while i < len(words) and ASSIGNMENT.fullmatch(words[i]):
        i += 1
    if words[:1] in (["for"], ["select"]) and "do" in words:  # for f do rm -rf $f; done
        words = words[words.index("do") + 1:]
    elif words[:1] in (["for"], ["select"]):  # for t in jq script: the list words are data
        return [], False
    name = lambda w: (w if "$" in w else os.path.basename(w)).lower()  # /usr/bin/time is time; ${SHELL##*/} stays
    while i < len(words) and (name(words[i]) in PREFIX_WORDS or (wrapper and WRAPPER_ARG.fullmatch(words[i]))):
        if wrapper == "command" and re.fullmatch(r"-p?[vV]p?", words[i]):  # command -v x looks x up, runs nothing
            return [], fed
        if name(words[i]) in WRAPPERS:
            wrapper = name(words[i])
            fed = fed or wrapper == "xargs"
        takes, opt, value = (option_value(words, i, WRAPPER_VALUES.get(wrapper, ())) if wrapper and words[i].startswith("-")
                             else (False, None, None))  # xargs -tI X, sudo -nD d: the bundle takes the next word
        if wrapper == "sudo" and opt in ("-D", "--chdir") and value is not None and moved is not None:
            moved.append(value)
        i += 2 if takes else 1
    rest = words[i:]
    lead = r"\d*(?:>>?|<(?!<)|&>>?)&?"  # not << or <<<: a document or here-string feeds the command
    while rest and re.fullmatch(lead + r"\S*", rest[0]):  # 2>&1 cd a, >x rm -rf b: a redirect may lead
        rest = rest[2:] if re.fullmatch(lead, rest[0]) else rest[1:]
    if rest and rest[0].startswith("=") and len(rest[0]) > 1:  # zsh =rm is the path of rm
        rest = [rest[0][1:]] + rest[1:]
    flag = next((k for k, w in enumerate(words[:i]) if re.fullmatch(r"-[a-zA-Z]*[IJ].*|--replace.*", w)), None) if fed else None
    repl = None if flag is None else (re.sub(r"^(?:-[a-zA-Z]*?[IJ]|--replace=?)", "", words[flag])
                                      or (words[flag + 1] if flag + 1 < len(words) else ""))  # -I@, -tI X, -J %
    if repl and repl != "{}" and not repl.startswith("="):  # xargs -I@ sh -c 'rm -rf @': @ is a fed target
        rest = [w.replace(repl, "{}") for w in rest]
    if rest and os.path.basename(rest[0]).lower() == "parallel":  # parallel rm -rf ::: x runs rm -rf x
        cmd = [w for w in rest[1:] if w not in (":::", "::::", ":::+")]
        while cmd and cmd[0].startswith("-"):  # parallel's own options come before the command
            cmd = cmd[2:] if cmd[0] in PARALLEL_VALUES else cmd[1:]
        if cmd and (" " in cmd[0] or "{" in cmd[0]):  # parallel 'rm -rf {}' ::: a, parallel {} ::: 'rm -rf a'
            cmd = (cmd[0].split() if "{" not in cmd[0].split()[0] else []) + [p for w in cmd[1:] for p in w.split()]
        inner, _ = strip(cmd, moved)
        return inner, True
    if rest and os.path.basename(rest[0]).lower() == "env":
        rest = rest[1:]
        while rest and (ASSIGNMENT.fullmatch(rest[0]) or rest[0].startswith("-")):
            if rest[0] in ("-S", "--split-string") and len(rest) > 1:  # env -S 'bash -c ...' splits its text
                rest = rest[1].split() + rest[2:]
                break
            takes, opt, value = option_value(rest, 0, ENV_VALUE_OPTS) if rest[0].startswith("-") else (False, None, None)
            if opt in ("-C", "--chdir") and value is not None and moved is not None:
                moved.append(value)
            rest = rest[2:] if takes else rest[1:]
        inner, inner_fed = strip(rest, moved)
        return inner, fed or inner_fed
    return rest, fed


def temp_roots():
    """The real paths of the temp folders: /tmp, /var/tmp and the TMPDIR, TEMP and TMP folders. A folder of one
    part (/ or C:/) is never a temp folder."""
    roots = {"/tmp", "/private/tmp", "/var/tmp"} | {os.environ.get(k) or "/tmp" for k in ("TMPDIR", "TEMP", "TMP")}
    return {r for r in (os.path.realpath(r) for r in roots) if os.path.dirname(r) != r}


def below(path, root):
    return path != root and path.startswith(root.rstrip(os.sep) + os.sep)


def own_tree(path, own, strict):
    """True when path lies in a linked worktree (its .git is a file) whose folder name carries own as one of its
    - separated parts; strict: inside the worktree, never its root."""
    folder = os.path.dirname(path) if strict else path
    while own:
        if own in os.path.basename(folder).lower().split("-") and os.path.isfile(os.path.join(folder, ".git")):
            return True
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent
    return False


def placed(path, own, strict):
    """True when a real path lies under a temp folder or in an own task worktree."""
    return any(below(path, root) for root in temp_roots()) or own_tree(path, own, strict)


class Line:
    """What one command line tells the delete guard: the session's own id, a process lookup anywhere in it, the
    variables it sets from a substitution, and those it sets once from a plain mktemp (a new temp folder)."""

    def __init__(self, command, own):
        self.own = own
        self.lookup = bool(LOOKUP.search(command))
        self.computed = set(re.findall(r"(?<![\w$])([A-Za-z_]\w*)=[\"']?(?:\$\(|`)", command))
        self.temp = {m.group(1) for m in MKTEMP.finditer(command)  # named once outside $NAME: no read, for, +=
                     if len(re.findall(r"(?<![\w${./-])%s(?![\w.-])" % m.group(1), command)) == 1
                     and not any("/" in w or w.split("=", 1)[0] in ("-p", "--tmpdir") for w in m.group(2).split())}


def place_reason(word, cwds, line, strict=True):
    """Why a folder delete of one target is refused, else None. A relative target is placed from every folder the
    line can be in (cwds; empty when a cd goes where the gate cannot follow). A glob is placed by each folder it
    matches. A file, a symlink (no trailing slash: the link goes, not its target) and a missing path pass."""
    if os.environ.get("TMPDIR"):  # "$TMPDIR/x" is the temp folder this session's shell names
        word = re.sub(r"^\$(?:TMPDIR\b|\{TMPDIR\})", lambda m: os.environ["TMPDIR"], word)
    var = re.match(r"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)\})", word)
    if var and (var.group(1) or var.group(2)) in line.temp:  # T=$(mktemp -d); rm -rf "$T": a new temp folder
        word = os.path.join(sorted(temp_roots())[0], "mktemp.guard") + word[var.end():]
    word = word[3:] or "." if word.startswith("~+/") or word == "~+" else word  # ~+ is the current folder
    if word.startswith("~-"):
        return f"cannot place {clip(word)}: it names the last folder."
    if word.startswith("~") and os.path.expanduser(word).startswith("~"):  # ~1, ~+1, zsh hash -d ~x
        return f"{clip(word)} names a folder stack entry: unplaceable."
    absolute = word.startswith(("/", "~"))
    if UNPLACEABLE.search(word) or (not absolute and not cwds):
        return f"cannot place {clip(word)}: a variable, brace or cd."
    for cwd in ([os.sep] if absolute else cwds):
        lexical = os.path.normpath(os.path.join(cwd, os.path.expanduser(word)))
        for path in (sorted(glob.glob(lexical)) if glob.has_magic(word) else [lexical]):
            leaf = os.path.join(os.path.realpath(os.path.dirname(path)), os.path.basename(path))
            if os.path.islink(leaf) and not word.endswith(("/", "/.")):
                continue
            real = os.path.realpath(path)
            if os.path.isdir(real) and not placed(real, line.own, strict):
                return f"{clip(real)} is not inside own worktree or temp."
    return None


def delete_args(head, args):
    """(recursive, targets) of one rm or trash call; trash of a folder always takes the folder whole."""
    recursive, targets, ended = head == "trash", [], False
    for a in args:
        if not ended and a == "--":
            ended = True
        elif not ended and a.startswith("--"):
            recursive = recursive or a == "--recursive"
        elif not ended and a.startswith("-") and len(a) > 1:
            recursive = recursive or bool(set(a[1:]) & {"r", "R"})
        else:
            targets.append(a)
    return recursive, targets


def kill_reason(head, args, line, fed):
    """Why one kill is refused, else None: a kill by name, file or group, of this shell or its parent, or by a pid
    computed in the line."""
    if head in ("pkill", "killall"):
        return NEVER_KILL
    if head == "fuser":
        kills = any(a == "--kill" or (a[:1] == "-" and a[1:2] != "-" and "k" in a) for a in args)
        return NEVER_KILL if kills else None
    if fed:
        return COMPUTED_KILL
    values, i, signal = [], 0, ""
    while i < len(args):
        a = args[i]
        i += 1
        if a == "--":
            values += args[i:]
            break
        if a in ("-l", "-L") or a.startswith("--list"):
            return None
        if a in ("-s", "-n"):
            signal = a + (args[i] if i < len(args) else "")
            i += 1
        elif a.startswith("-") and i == 1 and not re.fullmatch(r"-\$.*", a):  # the first dash word is the signal
            signal = a
        else:
            values.append(a)
    if any(v == "0" or re.fullmatch(r"-\d+", v) for v in values):
        return NEVER_KILL  # pid 0 is the process group, -1 every process, -N a group
    if signal in ("-0", "0", "-s0", "-n0"):
        return None  # signal 0 only checks that the process lives
    if any(re.search(r"\$\$|\$\{\$\}|\$\{?(?:PPID|BASHPID)\b", v) for v in values):
        return SELF_KILL
    if not values:
        return COMPUTED_KILL
    for v in values:
        if re.fullmatch(r"\d+", v) or v.startswith("%") or v == "$!":
            continue  # a literal pid, a job or the last background child of this shell
        var = re.fullmatch(r"\$(?:([A-Za-z_]\w*)|\{([A-Za-z_]\w*)\})", v)
        if v == "$" or "`" in v or "$(" in v or line.lookup or (var and (var.group(1) or var.group(2)) in line.computed):
            return COMPUTED_KILL
    return None


def clean_reason(folder, args, own):
    """Why a git clean is refused, else None: -x, -X or -d (not -n) with its folder or a pathspec outside an own
    task worktree (its root included) and the temp folders. folder is None when --work-tree, --git-dir or a GIT_DIR
    or GIT_WORK_TREE word moves it where the gate does not follow."""
    given = flags(args)
    if given & {"n", "--dry-run"} or not given & {"x", "X", "d"}:
        return None
    if folder is None:
        return ("git clean -x, -X or -d with --work-tree or GIT_DIR: cannot place its folder. "
                "Fix: git -C <your own worktree>.")
    specs, skip, ended = [], False, False
    for a in args:
        if skip:
            skip = False
        elif ended or not a.startswith("-"):
            specs.append(a)
        elif a == "--":
            ended = True
        elif a in ("-e", "--exclude"):
            skip = True
    for spec in specs or ["."]:
        path = os.path.realpath(os.path.join(folder, os.path.expanduser(spec)))
        if UNPLACEABLE.search(spec) or not placed(path, own, strict=False):
            return (f"git clean -x, -X or -d in {clip(path)}: not inside own worktree or temp. "
                    f"Fix: git clean -n to list.")
    return None


def drop_redirects(args):
    """args without redirect words (2>/dev/null, >out, > out, 2>): a redirect target is no pid and no path to delete."""
    out, skip = [], False
    for a in args:
        if skip:
            skip = False
            continue
        m = re.fullmatch(r"\d*(?:[<>]+)(.*)", a)
        if m:
            skip = not m.group(1)
            continue
        out.append(a)
    return out


def substitutions(command):
    """The command text of each $( ) and backtick pair that runs: a double-quoted one runs, a single-quoted or
    escaped one is text."""
    live = unquoted_scan(command)[1]
    found = [command[m.start(1):m.end(1)] for m in re.finditer(r"`([^`]*)`", live)]
    for m in re.finditer(r"\$\(", live):
        depth, j = 1, m.end()
        while j < len(live) and depth:
            depth += {"(": 1, ")": -1}.get(live[j], 0)
            j += 1
        found.append(command[m.end():j - 1 if depth == 0 else j])
    return found


def option_value(words, k, values):
    """(True when the option at words[k] takes the next word, the option's name, its value or None) under a command
    whose value options are values: -C dir, -Cdir, -iC dir (a bundle ends at its first value letter), --chdir dir,
    --chdir=dir."""
    word, after = words[k], (words[k + 1] if k + 1 < len(words) else None)
    if word.startswith("--"):
        opt, eq, value = word.partition("=")
        return (False, opt, value) if eq else (word in values, word, after if word in values else None)
    letters = "".join(o[1] for o in values if len(o) == 2)
    m = re.fullmatch(r"-[a-zA-Z0-9]*?([%s])(.*)" % letters, word, re.S) if letters else None
    return (not m.group(2), "-" + m.group(1), m.group(2) or after) if m else (False, None, None)


def find_reason(args, cwds, line):
    """The refusal of a find that deletes (-delete, or -exec rm -r or trash) under a root outside own trees and the
    temp folders, else None. Its roots are placed as folders, an own tree root included."""
    words = list(args)
    while words and words[0] in ("-H", "-L", "-P"):
        words = words[1:]
    roots = []
    for a in words:
        if a.startswith(("-", "(", "!")):
            break
        roots.append(a)
    at = next((k for k, a in enumerate(args) if a in FIND_EXEC), None)
    inner = strip([a for a in args[at + 1:] if a not in ("{}", "+", ";")])[0] if at is not None else []
    head = os.path.basename(inner[0]).lower() if inner else ""
    if inner and is_shell(inner[0]) and DELETE_TEXT.search(" ".join(inner[1:])):
        return DELETE_REFUSED + "find -exec runs a shell that deletes or kills; cannot place it." + DELETE_REMEDY
    deletes = "-delete" in args or (head in ("rm", "trash") and delete_args(head, inner[1:])[0])
    if deletes:
        why = next((w for w in (place_reason(r, cwds, line, strict=False) for r in roots or ["."]) if w), None)
        if why:
            return DELETE_REFUSED + f"find: {why}" + DELETE_REMEDY
    for k, a in enumerate(args):  # find own -exec rm -rf <peer> ;: each literal operand is a target too
        if a not in FIND_EXEC:
            continue
        end = next((j for j in range(k + 1, len(args)) if args[j] in (";", "+")), len(args))
        call = strip(args[k + 1:end])[0]
        name = os.path.basename(call[0]).lower() if call else ""
        if name in ("rm", "trash") and delete_args(name, call[1:])[0]:
            here = [] if a in ("-execdir", "-okdir") else cwds
            why = next((w for w in (place_reason(t, here, line) for t in delete_args(name, call[1:])[1] if "{}" not in t) if w), None)
            if why:
                return DELETE_REFUSED + f"find -exec {name}: {why}" + DELETE_REMEDY
    if deletes:
        return None
    return delete_reason(inner, cwds, line, True) if inner else None


def delete_reason(words, cwds, line, fed=False):
    """The refusal of a delete or kill outside the session's own work in one simple command, else None. cwds holds
    every folder the line can be in; it is empty when a cd goes where the gate cannot follow."""
    moved = []
    rest, piped = strip(words, moved)
    fed = fed or piped
    if not rest:
        return None
    for to in moved:  # env -C d rm -rf x, sudo -D d: place x from d
        folder = os.path.expanduser(to)
        if UNPLACEABLE.search(to) or to.startswith(("~-", "~+", "-")) or folder.startswith("~"):
            cwds = []  # env -C $d, env -C ~1: the gate cannot follow it
        else:  # an absolute folder places even after a cd the gate cannot follow
            cwds = [resolve(c, folder) for c in cwds or ([os.sep] if os.path.isabs(folder) else [])]
    while len(rest) > 1 and re.fullmatch(r'"?\$(?:\{[^}]*\}|[\w!@#?*$-]+)"?', rest[0]):  # $u rm -rf a: an empty word drops
        rest = rest[1:]
    rest = [re.sub(r'^"?\$(?:\{[^}]*\}|[\w!@#?*$-]+)"?(?=[A-Za-z])', "", rest[0])] + rest[1:]  # ${u}rm, "$u"rm
    head, args = os.path.basename(rest[0]).lower(), drop_redirects(rest[1:])
    if head == "find":
        return find_reason(args, cwds, line)
    if head in ("rm", "trash"):
        recursive, targets = delete_args(head, args)
        if not recursive:
            return None
        if fed or not targets:
            return DELETE_REFUSED + f"{head} takes its targets from a pipe or find; cannot place them." + DELETE_REMEDY
        why = next((w for w in (place_reason(t, cwds, line) for t in targets) if w), None)
        return DELETE_REFUSED + f"{head}: {why}" + DELETE_REMEDY if why else None
    if head in ("kill", "pkill", "killall", "fuser"):
        why = kill_reason(head, args, line, fed)
        return DELETE_REFUSED + why if why else None
    for cwd in cwds or [os.sep]:
        call = git_call([rest[0]] + args, cwd)
        if call is None:
            return None
        folder, sub, sub_args = call
        if sub == "worktree" and sub_args[:1] == ["remove"]:
            return DELETE_REFUSED + WORKTREE_REFUSED
        moved = any(w.split("=", 1)[0] in ("--work-tree", "--git-dir", "GIT_WORK_TREE", "GIT_DIR") for w in words)
        why = clean_reason(None if moved else folder, sub_args, line.own) if sub == "clean" else None
        if why:
            return DELETE_REFUSED + why
    return None


# The law-file shell fence: a shell write to a law file skips doc-gate rule 2 (the live hub) and
# rule 4 (the CLAUDE files), so Bash refuses it. Only ~/.claude (the per-seat global CLAUDE.md, doc-gate rule 4) passes.
LAW_NAMES = {"agents.md", "skill.md", "product-conventions.md", "claude.md", "claude.local.md"}
LAW_TOKEN = re.compile(r"(?i)[\w./~${}:\\-]*(?<![\w.-])(?:AGENTS|SKILL|PRODUCT-CONVENTIONS|CLAUDE(?:\.local)?)\.md(?![\w.-])")
LAW_REFUSED = "project-gate: law-file shell write refused: "
LAW_REMEDY = " Fix: use the Edit or Write tool in a hub task worktree; reads pass."
INTERPRETERS = re.compile(r"(?i)(?:python[\d.]*w?|py|node|perl|ruby)")
SCRIPT_WRITES = re.compile(  # a write call in an inline script (an earlier harness's doc-gate list, plus perl)
    r"\.write_(text|bytes)\s*\(|(?<!stdout)(?<!stderr)\.(write|writelines)\s*\(|"
    r"\.(touch|rename|symlink_to|hardlink_to)\s*\(|\.replace\(\s*['\"][^'\",]*\.md['\"]\s*\)|"  # Path.replace, not str.replace
    r"\bos\.(replace|rename|renames|link|symlink)\s*\(|\bshutil\.(copy\w*|move)\s*\(|"
    r"\binplace\s*=\s*(True|1)\b|\bopen\s*\([^\n]*?,\s*(mode\s*=\s*)?['\"][rbt]*[wax+]|"
    r"\.open\s*\(\s*(mode\s*=\s*)?['\"][rbt]*[wax+]|"
    r"\b(writeFile|appendFile|copyFile|rename|cp|symlink|link)(Sync)?\s*\(|\bcreateWriteStream\s*\(|"
    r"\bopen\s*\(?\s*(?:my\s+)?[\$\w]+\s*,\s*['\"]\+?>")
IN_PLACE = {"sed": re.compile(r"-[nrEsuz]*i.*|--in-place(?:=.*)?"), "gsed": re.compile(r"-[nrEsuz]*i.*|--in-place(?:=.*)?"),
            "perl": re.compile(r"-[0-9aclnpsTtuUWX]*i.*")}
COPY_VERBS = {"cp", "ln", "install", "mv"}


def law_target(word, cwd):
    """True when a shell write to word lands on a law file outside ~/.claude and outside a loose temp folder."""
    if os.path.basename(word).lower() not in LAW_NAMES:
        return False
    path = os.path.expanduser(os.path.expandvars(word))
    if "$" in path or not os.path.isabs(path):
        return True  # a variable, or a relative path a cd in the line can move: the name alone decides
    real = os.path.realpath(path)
    if below(real, os.path.realpath(os.path.expanduser("~/.claude"))):
        return False
    return not (any(below(real, root) for root in temp_roots()) and not in_checkout(real))


def in_checkout(path):
    """True when a folder above path holds .git or the hub marker ([project] marker_file): a scratch hub or a probe
    worktree under a temp folder keeps its law files fenced, a loose copy in a temp folder does not."""
    import harness_config
    marker = harness_config.load(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))["project"]["marker_file"]
    folder = os.path.dirname(path)
    while True:
        if os.path.exists(os.path.join(folder, ".git")) or os.path.exists(os.path.join(folder, marker)):
            return True
        parent = os.path.dirname(folder)
        if parent == folder:
            return False
        folder = parent


def redirect_targets(command):
    """The target word of each unquoted output redirect (>, >>, >|, &>, 2>), comments cut; a >( ) runs, it is no file."""
    found, quote, i, n = [], None, 0, len(command)
    while i < n:
        c = command[i]
        if quote != "'" and c == "\\":
            i += 2
            continue
        if quote is None and c == "#" and (i == 0 or command[i - 1] in " \t\n;&|("):
            while i < n and command[i] != "\n":
                i += 1
            continue
        if quote is None and c in "'\"":
            quote = c
        elif quote == c:
            quote = None
        elif quote is None and c == ">":
            j = i + 1
            while j < n and command[j] in ">|":
                j += 1
            if j < n and command[j] == "&":  # >&2 duplicates a descriptor; >&file is bash's &>file
                j += 1
            while j < n and command[j] in " \t":
                j += 1
            word, q = [], None
            while j < n and (q or command[j] not in " \t\n;&|()<>"):
                if q is None and command[j] in "'\"":
                    q = command[j]
                elif q == command[j]:
                    q = None
                else:
                    word.append(command[j])
                j += 1
            if word and not "".join(word).isdigit():
                found.append("".join(word))
            i = j
            continue
        i += 1
    return found


def copy_operands(args):
    """(sources, destination, True when -t names the destination folder) of a cp, ln, install or mv call."""
    operands, folder, k = [], None, 0
    while k < len(args):
        a = args[k]
        if a == "--":
            operands += args[k + 1:]
            break
        if a in ("-t", "--target-directory"):
            folder = args[k + 1] if k + 1 < len(args) else ""
            k += 2
            continue
        if a.startswith("--target-directory="):
            folder = a.split("=", 1)[1]
        elif a in ("-m", "--mode", "-o", "--owner", "-g", "--group", "-S", "--suffix"):
            k += 1  # install and cp options that take a value
        elif not a.startswith("-") or a == "-":
            operands.append(a)
        k += 1
    if folder is not None:
        return operands, folder, True
    if len(operands) < 2:
        return [], None, False
    return operands[:-1], operands[-1], False


def law_verb_reason(words, cwd, raw):
    """The refusal of a tee, sed -i or perl -i, cp, mv, ln or install onto a law file, or of an inline python, node,
    perl or ruby script that names one and writes, in one simple command; else None. raw holds the heredoc bodies a
    script with no script file reads from stdin."""
    run, _ = strip(words)
    if not run:
        return None
    head, args = os.path.basename(run[0]).lower(), drop_redirects(run[1:])
    operands = [a for a in args if not a.startswith("-")]
    if head == "tee" or (head in IN_PLACE and any(IN_PLACE[head].fullmatch(a) for a in args)):
        hit = next((a for a in operands if law_target(a, cwd)), None)
        return LAW_REFUSED + f"{head} writes {clip(hit)}." + LAW_REMEDY if hit else None
    if head in COPY_VERBS:
        sources, dest, into = copy_operands(args)
        if dest is None:
            return None
        to_folder = into or dest.endswith("/") or os.path.isdir(os.path.join(cwd, os.path.expanduser(dest)))
        landed = [os.path.join(dest, os.path.basename(s)) for s in sources] if to_folder else [dest]
        hit = next((w for w in landed + (sources if head == "mv" else []) if law_target(w, cwd)), None)
        return LAW_REFUSED + f"{head} writes or moves {clip(hit)}." + LAW_REMEDY if hit else None
    if not INTERPRETERS.fullmatch(head):
        return None
    inline = any(re.fullmatch(r"-[A-Za-z]*[ce]", a) for a in args)
    text = " ".join(run[1:])
    if not inline and (not operands or operands[0] == "-"):
        text += "\n" + "\n".join(m.group(3) for m in documents(raw))
    if not SCRIPT_WRITES.search(text):
        return None
    home = os.path.expanduser("~/.claude/")
    hit = next((m.group(0) for m in LAW_TOKEN.finditer(text)
                if not m.group(0).startswith(("~/.claude/", "$HOME/.claude/", "${HOME}/.claude/", home))), None)
    return (LAW_REFUSED + f"the {clip(head, 12)} script names {clip(hit)} and writes a file." + LAW_REMEDY) if hit else None


def law_write_reason(command, words_list, cwd, raw):
    """The law-file shell fence over one command text: a redirect onto a law file, then each simple command."""
    hit = next((t for t in redirect_targets(command) if law_target(t, cwd)), None)
    if hit:
        return LAW_REFUSED + f"a redirect writes {clip(hit)}." + LAW_REMEDY
    return next((r for r in (law_verb_reason(w, cwd, raw) for w in words_list or []) if r), None)


FILTERED = "{} changes the echoed text before a shell runs it; the gate cannot read it."


def line_texts(found, raw, decode=True, command=""):
    """Every text the line produces, in line order: each echo, print, printf and yes, each here-string, each
    process substitution body's text (split() reads its commands in place), then each heredoc body."""
    texts, fed = [], bool(re.search(r"(?:\$\(|`)[^)`]*<<-?\s*['\"]?\w", command))  # eval "$(cat <<EOF ...)"
    docs = iter(documents(raw or ""))
    for k, (_, words) in enumerate(found):
        if any(re.fullmatch(r"\d*<<-?\S*", w) for w in words):  # { echo 'cd a'; cat <<EOF ... EOF; } | bash: in order
            doc = next(docs, None)
            to_file = any(re.fullmatch(r"\d*>>?\S*", w) and not re.fullmatch(r"\d*>&\d*", w) for w in words)
            if doc is not None and not to_file and stage_name(words) in PASS_STAGES:
                body = doc.group(3) if doc.group(1) or not decode else re.sub(
                    r"\\([\\$`\n])", lambda e: "" if e.group(1) == "\n" else e.group(1), doc.group(3))
                texts.append(body)
                continue
        text = echoed(words, decode)
        here = here_string(strip(words)[0])
        here = here if here is not None else here_string(words)  # parallel <<< 'x', sudo -s <<< 'x'
        texts.append(text if text is not None else here + "\n" if here is not None else "")
        piped = k + 1 < len(found) and PIPE.fullmatch(found[k + 1][0])
        if any(re.fullmatch(r"\d*<<-?\S*", w) for w in words) and (piped or runs_code(words) or words[0] in ("exec", "}", ")")
                                                                   or found[k][0].endswith("(")):  # $(cat <<EOF)
            fed = True  # cat <<EOF | bash, bash <<EOF, { bash; } <<EOF, exec 3<<EOF: the document can reach a shell
    bodies = [m.group(3) if m.group(1) or not decode else re.sub(r"\\([\\$`\n])", lambda e: "" if e.group(1) == "\n" else e.group(1), m.group(3))
              for m in documents(raw)]  # an unquoted document drops \newline and reads \\ as \
    return [t for t in texts if t] + (bodies if fed else [])


def set_events(found):
    """Each time the line sets a variable, by name: x=, x+=, read, mapfile, for, select, printf -v, declare."""
    for _, words in found:
        run, _ = strip(words)
        for w in words:
            m = re.fullmatch(r"([A-Za-z_]\w*)(?:\[[^]]*\])?\+?=.*", w, re.S)
            if m:
                yield m.group(1)
        head = run[0] if run else ""
        if head in ("read", "mapfile", "readarray", "getopts"):
            yield from (w for w in run[1:] if re.fullmatch(r"[A-Za-z_]\w*", w))
        if words[:1] and words[0] in ("for", "select") and len(words) > 1:
            yield words[1]
        if head == "printf" and "-v" in run[:-1]:
            yield run[run.index("-v") + 1]
        if head == "getopts":
            yield "OPTARG"
        for w in words:  # : ${x:=rm} sets x
            yield from re.findall(r"\$\{([A-Za-z_]\w*):?=", w)


def set_names(found):
    """The variables the line sets (set_events)."""
    return set(set_events(found))


EXPANSION = re.compile(r"\$\{?[!#]?([A-Za-z_]\w*)")
POSITIONAL = re.compile(r"\$\{?[!#]?[@*0-9]")  # "$@", $1, ${*}: text the call's own words fill
INEXACT = "the fed text holds a form the gate cannot read exactly (printf width, \\c, non-POSIX shell)."
EXEC_TEXT = {"awk": re.compile(r"system\s*\(|\|\s*\"|\|&|\|\s*getline"), "sed": re.compile(r"/[\w]*e[\w]*(?:\s|;|$)|(?:^|[;\n{])\s*[\d,$]*\s*e(?:\s|$)")}


EXACT_ESCAPE = re.compile(r"\\(?:[nrtabfvEe\\'\"?]|0?[0-7]{1,3}|x[0-9a-fA-F]{1,2}(?!\{)|u[0-9a-fA-F]{1,4}|U[0-9a-fA-F]{1,8})")
NUL_ESCAPE = re.compile(r"\\(?:0+(?![0-7])|x0{1,2}(?![0-9a-fA-F])|u0{1,4}(?![0-9a-fA-F])|U0{1,8}(?![0-9a-fA-F]))")


def odd_escape(text):
    """True when text holds a backslash escape that bash and zsh decode differently or the gate does not decode
    exactly: a NUL (zsh keeps the rest, bash cuts it), \\x or \\u with no digits, \\x{..}, \\C-, \\M-, or an unknown
    escape such as \\m or \\-."""
    if NUL_ESCAPE.search(text) or "\\c" in text.replace("\\\\", ""):  # \c cuts or makes a control character
        return True
    for m in ESCAPE_C.finditer(text.replace("\\\\", "")):  # \400, \0001, \x001: a NUL by value, bash cuts there
        if (m.group(2) and int(m.group(2), 8) & 0xFF == 0) or (m.group(1) and int(m.group(1), 16) == 0):
            return True
    rest = EXACT_ESCAPE.sub("", text.replace("\\\\", ""))
    return "\\" in rest.rstrip("\\") or bool(re.search(r"\\.", rest))


def inexact(words):
    """True when a producer writes text the gate does not read exactly: a printf conversion other than %s, %b, %d,
    %c and %% (a width, precision, * or length), or \\c, an escaped backslash or a trailing backslash in the text."""
    run, _ = strip(words)
    head = os.path.basename(run[0]).lower() if run else ""
    here = here_string(run)
    if head not in ("echo", "print", "printf", "yes") and here is None:
        return False
    args = [here] if here is not None else drop_redirects(run[1:])
    fmt = args[1] if args[:1] == ["--"] and len(args) > 1 else args[0] if args else ""
    if head == "printf" and re.search(r"%(?!%|[sbdc])", fmt):
        return True
    return any(re.search(r"\\c|\\\\|\\$", a) or odd_escape(a) for a in args)
UNREAD = "{} runs code from a variable; the gate cannot read it."


def splice_body(body, fed):
    """A -c body with the text it reads from stdin written in where it reads it: "$(cat)", source /dev/stdin and a
    read NAME then $NAME (echo rm | bash -c '$(cat) -rf a' runs rm -rf a)."""
    line = fed.rstrip("\n")
    # Any substitution can read stdin ($(cat -u), $(tee), $(head -n1), `cat`): each one that is not produced text is
    # written as the fed text, and so is an inner shell that reads it (bash -c 'cd a; bash').
    out = re.sub(r'"?(?:\$\((?![^()]*\b(?:echo|printf|print)\b)[^()]*\)|`[^`]*`)"?', lambda m: line, body)
    out = re.sub(r"(?:\bsource|(?<![\w.])\.)\s+/dev/[^\s;&|)]+", lambda m: line, out)
    out = re.sub(r"(?:^|(?<=[;(])|(?<=&&))\s*(?:[\w./-]*/)?(?:ba|z|da|k|c|tc)?sh\s*(?=$|[;&|)])|(?:^|(?<=[;(])|(?<=&&))\s*\"?\$\{?SHELL\}?\"?\s*(?=$|[;&|)])",
                 lambda m: " " + line + " ", out)
    for name in re.findall(r"\bread\s+(?:-\w+\s+)*([A-Za-z_]\w*)", out):
        out = re.sub(r"\bread\s+(?:-\w+\s+)*%s\b" % name, ":", out)
        out = re.sub(r'"?\$(?:\{%s\}|%s\b)"?' % (name, name), lambda m: line, out)
    return out


def spliced_line(found, raw="", command=""):
    """The line as the current shell runs it: each source or . of a process substitution or of /dev/stdin, and each
    eval, written as the code it runs, in place (source <(echo 'cd a'); rm -rf build)."""
    parts, k, changed = [], 0, False
    evals = iter(re.findall(r"\beval\s+(?:--\s+)?((?:\$\([^()]*\)|[^;&|\n])*)", command))
    while k < len(found):
        sep, words = found[k]
        run, _ = strip(words)
        head = os.path.basename(run[0]).lower() if run else ""
        line_feed = "".join(piped_texts(found, k)) or "".join(line_texts(found, raw, True, command))
        if head in ("source", ".") and any(stdin_path(w) for w in run[1:]):  # a here-string, document or exec feeds it
            parts.append(line_feed)
            changed = True
        elif head in ("source", ".") and any(w in ("<", "=") for w in run[1:]):
            body, k = [], k + 1
            while k < len(found) and ")" not in found[k][0]:  # the body ends at the ) separator (;); too)
                body.append(echoed(found[k][1]))
                k += 1
            parts.append("".join(b or "" for b in body) if all(b is not None for b in body) else line_feed)  # <(cat <<EOF)
            changed = True
            continue
        elif head == "eval":
            args = run[2:] if run[1:2] == ["--"] else run[1:]
            raw_args = next(evals, None)  # eval $(cat) -rf a: an unquoted substitution splits the words, so read the text
            args = [raw_args] if raw_args and "$(" in raw_args and not any("$(" in a for a in args[1:]) and (
                k + 1 < len(found) and "(" in found[k + 1][0]) else args
            fed = line_feed.rstrip("\n")  # eval "$(cat <<EOF ... EOF)": the document is the code
            code = re.sub(r"\$\(\s*cat\s*<<-?\s*(['\"]?)(\w+)\1[^\n]*\n(.*?)\n\s*\2\s*\n?\)", lambda m: m.group(3), " ".join(args), flags=re.S)
            parts.append(re.sub(r'"?(?:\$\((?![^()]*\b(?:echo|printf|print)\b)[^()]*\)|`[^`]*`)"?', lambda m: fed, code))  # $(tee), `cat`
            changed = True
        elif words and words[0] not in ("}", ")", "done", "fi"):
            parts.append(shlex.join(words))
        k += 1
    return "\n".join(p for p in parts if p.strip()) if changed else None


def feed_reason(command, found, raw, event, cwds):
    """The refusal of a line that runs text as code, else None. In a line with a shell or code
    runner, every text the line produces is judged as one script, in line order, both escape-decoded and as
    written, wherever its pipe, group, redirect or process substitution sends it; each -c body is judged before
    that text and with its positional words; a stage that changes the text on its way is refused; and code read
    from a variable the line sets that the gate cannot resolve is refused when it stands in command position or
    the line holds a delete word."""
    names = set_names(found)
    unresolved = {n for n in names - set(variables(found))} | {n for n in names if re.search(  # ${y#x}, a=(r m), a[0]=r
        r"\b%s(?:\[|=\()|\$\{[!#]?%s[^}\w]" % (re.escape(n), re.escape(n)), command)}
    for m in re.finditer(r"\[\s*\$\(([^()]*)\)", command):  # let 'a[$(rm -rf x)]=1': a subscript runs its substitution
        body = reason(m.group(1), event, "", cwds)
        if body:
            return body
    read_only = {w for _, ws in found for w in strip(ws)[0][1:] if strip(ws)[0][:1] in (["read"], ["mapfile"], ["readarray"])
                 and re.fullmatch(r"[A-Za-z_]\w*", w)} - {m.group(1) for m in re.finditer(r"(?<![\w$])([A-Za-z_]\w*)\+?=", command)}
    if not (any(PIPE.fullmatch(sep) for sep, _ in found[1:]) or re.search(r"<<|[<>=]\(|<&", command)):
        unresolved -= read_only  # bash -c 'read x; eval "$x"' alone reads the caller's stdin, judged where it is fed
    if unresolved:  # a=r; a+=m; $a -rf x, trap "$a -rf x" EXIT, find . -exec $a -rf {} +: a command word it cannot fill
        alt = "|".join(map(re.escape, sorted(unresolved)))
        spots = r"(?:^|[;&|(\n]|\b(?:do|then|else|exec|-exec|-execdir|trap|eval|xargs|sudo|env|nohup|time)\s)\s*[\"']?\$\{?(?:%s)\b"
        echoes = r"\b(?:echo|print|printf|cat\s+<<<)\s+(?:-\w+\s+)*[\"']?\$\{?(?:%s)\b"
        fed = any(stdin_shell(w) for _, w in found)
        if re.search(spots % alt, command) or (fed and re.search(echoes % alt, command)) or (
                fed and re.search(r"<<<\s*[\"']?\$\{?(?:%s)\b" % alt, command)):
            return DELETE_REFUSED + UNREAD.format("a command word") + DELETE_REMEDY
    for _, words in found:  # awk 'BEGIN{system("rm -rf a")}', sed '1e rm -rf a': the program runs a command
        name = stage_name(words)
        exec_text = EXEC_TEXT.get(name[1:] if name in ("gawk", "mawk", "nawk", "gsed") else name)
        args = " ".join(strip(words)[0][1:])
        seen = (args, unescape(args), re.sub(r'"\s*"', "", args))  # system("r" "m -rf a"), r\x6d
        exec_text = exec_text or (re.compile(r"getline") if name.endswith("awk") else None)
        if exec_text and exec_text.search(args) and any(DELETE_TEXT.search(s) for s in seen):
            return DELETE_REFUSED + f"{clip(name, 24)} runs a command from its program text; cannot place it." + DELETE_REMEDY
    visible = any(DELETE_TEXT.search(t) for t in (command, unescape(command), re.sub(r"[\\'\"]", "", command),
                                                  re.sub(r"[^\w/.]", " ", command)))  # ${x:-rm}
    if visible and re.search(r"(?:^|[;&|(\n]|\b(?:do|then|else|exec|-exec|-execdir|xargs|sudo|env|nohup|time)\s)\s*[\"']?(?:\$\(|`)",
                             re.sub(r"\$\(\s*(?:cat\b[^()]*|<\s*/dev/stdin)\)", "", command)):  # $(which rm) -rf a; $(cat) is the fed text
        return DELETE_REFUSED + UNREAD.format("a substitution in command position") + DELETE_REMEDY
    runners = [w for _, w in found if stdin_shell(w)]
    # A text reaches a shell only through a pipe, a here-string, a document, a process substitution or a redirect:
    # echo 'rm -rf x' || bash runs no text.
    written = set(redirect_targets(command))  # echo 'rm -rf a' > s.sh; bash s.sh: the file the line writes runs
    reused = any(command.count(os.path.basename(f)) > 1 for f in written if os.path.basename(f))  # cd /tmp; sh x.sh
    reach = reused or written & {w for _, words in found if stdin_shell(words) for w in words} or any(
        PIPE.fullmatch(found[k][0]) and reaches(found, k - 1) for k in range(1, len(found))) or re.search(  # a pipe into a runner
        r"<<|[<>=]\(|<&|\d<|>&\s*\$|mkfifo|coproc", command)
    texts = line_texts(found, raw, True, command) if reach else []
    if reused and raw:  # cat <<'EOF' > x.sh ... sh x.sh: the document is the script
        texts += [m.group(3) for m in documents(raw) if m.group(3) not in texts]
    if not runners and not any(stage_name(w) in TEXT_RUNNERS for _, w in found):
        return None
    raw_texts = line_texts(found, raw, False, command) if reach else []
    scripts = [s for s in ("".join(texts), "".join(raw_texts)) if s]  # echo '\cd a; rm -rf b': bash echo keeps \c as text
    texts = texts or raw_texts
    for words in runners:
        run = shell_run(words)
        flag = next((k for k, w in enumerate(run[:-1]) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", w)), None)
        if flag is not None and is_shell(run[0]):
            body, args = run[flag + 1], run[flag + 2:]
            spliced = [splice_body(body, s) for s in scripts[:2]]
            scripts += [x for x in spliced if x != body]
            scripts += [x + "\n" + s for x, s in zip(spliced, scripts[:2])] + [s + "\n" + x for x, s in zip(spliced, scripts[:2])]
            if "xargs" in [w.lower() for w in words]:  # xargs -I{} sh -c '{} -rf a', xargs sh -c '$0 -rf a': fed words fill it
                fed_words = (scripts[0].split() if scripts else [])
                scripts += [body.replace("{}", s.strip()) for s in scripts[:2]]
                args = args + fed_words
            scripts += (["\n".join(args), " ".join(args), " ".join(args[1:])] if args else [])
            if args:  # sh -c '$0 -rf $1' rm a: the body with its positional words written in
                filled = re.sub(r'"?\$(?:\{([0-9@*])\}|([0-9@*]))"?', lambda m: (" ".join(args[1:]) if (m.group(1) or m.group(2)) in "@*"
                                else (args[int(m.group(1) or m.group(2))] if int(m.group(1) or m.group(2)) < len(args) else "")), body)
                scripts.append(filled)
                for cut in range(2, len(args)) if re.search(r"\bshift\b", body) else ():  # cd "$1"; shift; "$@"
                    scripts.append(re.sub(r'"?\$(?:\{[@*]\}|[@*])"?', lambda m: " ".join(args[cut:]), filled.replace(" ".join(args[1:]), " ".join(args[cut:]))))
                    scripts.append(re.sub(r'"?\$(?:\{[@*]\}|[@*])"?', lambda m: " ".join(args[cut:]), body.replace("$1", args[1] if len(args) > 1 else "")))
    whole = spliced_line(found, raw, command)  # source <(echo 'cd a'); rm -rf build: the fed code moves the line's own delete
    scripts += [whole] if whole else []
    for words in runners:  # sudo -s 'rm -rf a' runs its words through a shell
        lowered = [w.lower() for w in words]
        if "sudo" in lowered and {"-s", "-i", "--shell", "--login"} & set(lowered):
            rest = drop_redirects(strip(words)[0])
            scripts += [" ".join(rest)] if rest else []
    for script in scripts:
        body = reason(script, event, "", cwds) if script.strip() else None
        if body:
            return body
    if not runners:
        return None
    if texts and any(inexact(w) for _, w in found) or re.search(r"<<-?\s*\w[^\n]*\n(?:.*\\\n)", raw or ""):
        return DELETE_REFUSED + INEXACT + DELETE_REMEDY
    if visible and texts and any(NON_POSIX.fullmatch(os.path.basename(shell_run(w)[0]).lower()) for w in runners
                                 if shell_run(w)) and any("$" in t or "set " in t for t in texts):
        return DELETE_REFUSED + INEXACT + DELETE_REMEDY  # echo 'set x = rm; $x -rf a' | csh
    live_cmd = unquoted_scan(command)[1]
    if visible and len(live_cmd) == len(command) and any(  # bash <(sh -c 'echo rm -rf a'): a shell writes the script
            any(runs_code(w) for w in segments(command[s + 2:e - 1]) or []) for s, e in spans(live_cmd, "<(") + spans(live_cmd, "=(")):
        return DELETE_REFUSED + "the output of one shell runs in another; the gate cannot read it." + DELETE_REMEDY
    if visible and re.search(r"\benv\b[^|;&\n]*\s(?:-C|--chdir)|\bsudo\b[^|;&\n]*\s(?:-D|--chdir)", command):
        return DELETE_REFUSED + "env -C or sudo -D moves the folder a shell starts in; cannot place its delete." + DELETE_REMEDY
    if visible and re.search(r"\b(?:PS[0-9]|PROMPT_COMMAND|BASH_ENV|ENV|SHELLOPTS)=", command):
        return DELETE_REFUSED + "a shell start-up variable runs text the gate cannot read." + DELETE_REMEDY
    for k, (sep, words) in enumerate(found):  # ls | sed 's|^|rm -rf |' | sh: a middle stage writes the script
        chain = k + 1 < len(found) and PIPE.fullmatch(found[k + 1][0])
        later = (pipe_reach(found, k) if chain else []) or []  # git x | tail; (bash ...): tail's output reaches no shell
        later = found[k + 1:] if chain and pipe_reach(found, k) is None else later
        if chain and stdin_shell(words) and any(stdin_shell(w) for _, w in later) and (
                visible or any(re.search(r"[%\\]", t) for t in texts)):  # echo 'echo rm -rf a' | bash | bash
            return DELETE_REFUSED + "the output of one shell runs in another; the gate cannot read it." + DELETE_REMEDY
        if not chain or stdin_shell(words) or not any(stdin_shell(w) for _, w in later):
            continue
        name = stage_name(words)
        first = not (k and PIPE.fullmatch(sep))
        if name and not first and name not in PASS_STAGES and echoed(words) is None:
            return DELETE_REFUSED + FILTERED.format(clip(name, 24)) + DELETE_REMEDY
        if name and first and name not in PASS_STAGES and echoed(words) is None and here_string(words) is None and (
                visible or re.search(r"[%\\]", " ".join(words[1:]))):  # date '+rm -rf a' | sh
            return DELETE_REFUSED + f"{clip(name, 24)} writes the text a shell runs; the gate cannot read it." + DELETE_REMEDY
    if texts:
        stages = [w for k, (sep, w) in enumerate(found) if (PIPE.fullmatch(sep) and k
                  or k + 1 < len(found) and PIPE.fullmatch(found[k + 1][0])) and (  # a stage a pipe feeds or reads
                      written or reaches(found, k))]  # whose text can reach a shell
        fed = [w for _, w in found if here_string(strip(w)[0]) is not None or any(re.fullmatch(r"\d*<<-?\S*", x) for x in w)]
        changed = next((stage_name(w) for w in stages + fed if stage_name(w) and echoed(w) is None and not stdin_shell(w)
                        and stage_name(w) not in PASS_STAGES), None)
        if changed:
            return DELETE_REFUSED + FILTERED.format(clip(changed, 24)) + DELETE_REMEDY
    names = set_names(found)
    for site in texts if visible else []:  # echo "${x:-}" | bash, y=$x; echo "$y" | bash: a fed text the gate cannot fill
        if (any(n in unresolved for n in EXPANSION.findall(site)) or POSITIONAL.search(site)
                or re.search(r"(?:^|[;&|\n])\s*[\"']?\$", site)):  # echo ${x:-rm} -rf a | bash
            return DELETE_REFUSED + UNREAD.format("the fed text") + DELETE_REMEDY
    for words in runners:
        run = shell_run(words)
        if not run:
            continue
        flag = next((k for k, w in enumerate(run[:-1]) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", w)), None)
        args = run[1:] if os.path.basename(run[0]).lower() in ("eval", "source", ".") else []
        args = args[1:] if args[:1] == ["--"] else args
        # (site, positional words it can read): a -c body's positional words are judged above as scripts.
        sites = ([(run[0], r"\$\{?[@*1-9]")] if "$" in run[0] else []) + ([(" ".join(args), POSITIONAL.pattern)] if args else []) + (
            [(run[flag + 1], None if run[flag + 2:] else POSITIONAL.pattern)] if flag is not None else [])
        for site, positional in sites:
            hit = [n for n in EXPANSION.findall(site) if n in unresolved] or (visible and positional and re.findall(positional, site))
            first = EXPANSION.match(site.lstrip(" \"'"))
            if hit and (visible or (first and first.group(1) in unresolved) or re.match(r"\s*[\"']?\$", site)):
                return DELETE_REFUSED + UNREAD.format(clip(stage_name(words) or run[0], 24)) + DELETE_REMEDY
            bare = re.sub(r"\$\(\s*(?:cat\b[^()]*|<\s*/dev/stdin)\)", "", site)  # $(cat) reads the fed text, judged above
            if visible and re.search(r"\$\(|`", bare):  # eval "$(date '+rm -rf a')", $(which rm) -rf a
                return DELETE_REFUSED + UNREAD.format(clip(stage_name(words) or run[0], 24)) + DELETE_REMEDY
    return None


def reason(command, event, raw=None, cwds=None):
    """The refusal for one Bash command (heredoc bodies cut), else None. It judges command words, so a quoted
    mention passes; a text that does not parse falls back to the text rules. raw is the command
    with its heredoc bodies, read from the event when None; a body fed to a shell is judged as commands. cwds is the
    folder set a shell body starts in, the event cwd when None."""
    shown = (command, re.sub(r"[\\'\"]", "", command), re.sub(r"[^\w/.]", " ", command), unescape(command, "c"),
             re.sub(r"[\\'\"]", "", NUL_ESCAPE.sub("", command)))  # r\0m, cd a\0\nrm
    if any(odd_escape(m.group(1)) for m in re.finditer(r"(?<![\\\w])\$'((?:[^'\\]|\\.)*)'", command)) and any(
            DELETE_TEXT.search(s) for s in shown):  # $'r\0m', $'\-rf', $'cd a\C-Jrm -rf b': zsh and bash part ways
        return DELETE_REFUSED + INEXACT + DELETE_REMEDY
    command = ansi_c(command)
    worded = word_forms(command)  # rm${IFS}-rf a, {rm,-rf,a}: judged as bash splits and expands them, and as written
    live = unquoted_scan(worded)[1]
    if len(live) == len(worded):  # r${u}m -rf a: an unset name splits no word, so bash runs rm
        cuts = [m.span() for m in re.finditer(r"(?<=[A-Za-z])(?:\$\{[^}]*\}|\$\([^()]*\)|`[^`]*`|\$[@*#?!$0-9]|\$[A-Za-z_]\w*)"
                                              r"|\"(?:\$\{[^}]*\}|\$[A-Za-z_]\w*|\$[@*#?!$0-9])\"(?=[A-Za-z])", live)]  # "$u"rm
        for s, e in reversed(cuts):
            worded = worded[:s] + worded[e:]
    if worded != command:
        body = reason(worded, event, raw, cwds)
        if body:
            return body
    found = segments(command)
    if found is None and "\n" in command:  # bash runs each line before the one that does not parse
        lines = command.split("\n")
        cut = next((i for i in range(1, len(lines) + 1) if segments("\n".join(lines[:i])) is None), len(lines))
        body = reason("\n".join(lines[:cut - 1]), event, "", cwds) if cut > 1 else None
        if body:
            return body
    if found is None:
        law = law_write_reason(command, None, str(event.get("cwd") or os.getcwd()), "")
        return law or (FILTER_REFUSED if FILTER.search(command) else ORIGINAL_REFUSED if ORIGINAL_DELETE.search(command)
                else NO_VERIFY_REFUSED if NO_VERIFY_TEXT.search(command)
                else CONFORM_REFUSED if CONFORM_TEXT.search(command)
                else OVERRIDE_REFUSED if HOOKS_TEXT.search(command)
                else DELETE_REFUSED + UNPARSED if DELETE_TEXT.search(command) or DELETE_TEXT.search(re.sub(r"[\"'\\`]", "", command))
                else None)
    if raw is None:
        tool_input = event.get("tool_input")
        raw = str(tool_input.get("command") or "") if isinstance(tool_input, dict) else ""
        for m in documents(raw):  # cat <<EOF with $(rm -rf a) in it: an unquoted document runs its substitutions
            for s in substitutions(m.group(3)) if not m.group(1) else ():
                body = reason(s, event, "", cwds) if s.strip() else None
                if body:
                    return body
    cwd = str(event.get("cwd") or os.getcwd())
    # Every folder the line can be in: a ( ) subshell, a && or || branch and popd make the last cd no proof, so a
    # relative delete is placed from each (a Flight review). Empty once a cd goes where the gate cannot follow.
    cwds = [cwd] if cwds is None else list(cwds)
    law = law_write_reason(command, found, cwd, raw)
    if law:
        return law
    line = Line(command, str(event.get("session_id") or "")[:8].lower())
    body = next((r for r in (reason(s, event, "", cwds) for s in substitutions(command) if s.strip()) if r), None)
    if body:
        return body
    expanded, changed = expand(command)  # $(echo rm) -rf x: the echoed text runs as code
    if changed and any(runs_code(w) for w in found):
        return DELETE_REFUSED + FILTERED.format(clip(changed, 24)) + DELETE_REMEDY
    body = reason(expanded, event, "", cwds) if expanded != command else None
    if body:
        return body
    as_written = expand(command, decode=False)[0]  # eval "$(echo '\rm -rf a')": bash echo keeps \r as text
    body = reason(as_written, event, "", cwds) if as_written not in (command, expanded) else None
    if body:
        return body
    original_seen = False  # an earlier command of the text names refs/original: a pipe into xargs deletes it
    for k, words in enumerate(found):
        moved = strip(words)[0] or words  # { cd a; }, do cd a, builtin cd a, command cd a
        verb = moved[0].lower()
        steps = [moved] if verb in ("cd", "pushd", "popd", "chdir") else []
        if verb == "eval":  # eval 'cd a'; echo 'rm -rf b' | bash: the eval moves the line
            code = moved[2:] if moved[1:2] == ["--"] else moved[1:]
            steps = [s for s in (segments(" ".join(code)) or [["cd", "$unknown"]]) if s and
                     (strip(s)[0] or s)[0].lower() in ("cd", "pushd", "popd", "chdir")]
            steps = [strip(s)[0] or s for s in steps]
        for step in steps:
            if step[0].lower() != "popd":
                given = [w for w in step[1:] if w not in ("-L", "-P", "-e", "-@", "--")]
                target = given[0] if given else "~"
                target = cwd + target[2:] if target == "~+" or target.startswith("~+/") else target  # ~+ is the current folder
                cwd = resolve(cwd, target)
                unknown = target.startswith(("-", "+", "~-")) or UNPLACEABLE.search(target) or re.search(r"[*?[]", target) or (
                    target.startswith("~") and os.path.expanduser(target).startswith("~"))  # cd ~1, cd ~x: a stack entry or named folder
                cwds = [] if unknown or not cwds else list(dict.fromkeys(cwds + [resolve(c, target) for c in cwds]))
        if verb in ("cd", "pushd", "popd", "chdir"):
            continue
        head = os.path.basename(next((w for w in words if not re.fullmatch(r"[A-Za-z_]\w*=.*", w)), "")).lower()
        bypass = hook_bypass_reason(words)
        if bypass:
            return bypass
        run, _ = strip(words)  # sudo bash -c, nohup sh -c, env A=1 bash -c: the shell the wrapper runs
        shell = os.path.basename(run[0]).lower() if run else ""
        flag = next((k for k, w in enumerate(run[:-1]) if re.fullmatch(r"-[a-zA-Z]*c[a-zA-Z]*", w)), None)
        inner = (run[flag + 1] if run and is_shell(run[0]) and flag is not None
                 else " ".join(run[2:] if run[1:2] == ["--"] else run[1:]) if shell == "eval" else None)
        body = reason(inner, event, "", cwds) if inner else None
        if body:
            return body
        operands = [w for w in drop_redirects(run[1:]) if not w.startswith("-")]
        if run and is_shell(run[0]) and flag is None and not operands:  # bash <<EOF, cat <<EOF | bash: the document runs
            body = next((r for r in (reason(m.group(3), event, "", cwds) for m in documents(raw)) if r), None)
            if body:
                return body

        if run and re.search(r"[*?[]", run[0]) and any(fnmatch.fnmatchcase(v, os.path.basename(run[0]).lower())
                                                       for v in ("rm", "trash", "kill", "pkill", "killall", "fuser", "find", "git")):
            return DELETE_REFUSED + f"{clip(run[0], 24)} is a pattern that can name a delete or kill; cannot place it." + DELETE_REMEDY
        trap = [w for w in run[1:] if w != "--"]
        if shell == "trap" and trap:  # trap 'rm -rf a' EXIT runs its text
            body = reason(trap[0], event, "", cwds)
            if body:
                return body
        names_original = any("refs/original" in w for w in words)
        if head.startswith("git-filter-"):
            return FILTER_REFUSED
        if (head in ("rm", "trash", "unlink", "rmdir") and names_original) or (head == "xargs" and original_seen):
            return ORIGINAL_REFUSED
        original_seen = original_seen or names_original
        why = delete_reason(words, cwds, line)
        if why:
            return why
        call = git_call(words, cwd)
        if call is None:
            continue
        folder, sub, args = call
        if sub in ("filter-branch", "filter-repo"):
            return FILTER_REFUSED
        if sub == "update-ref" and flags(args) & {"d", "--delete"} and original_seen:
            return ORIGINAL_REFUSED
        if sub == "branch":
            why = branch_reason(folder, args, line.own)
            if why:
                return why
            continue
        if sub in ("checkout", "switch"):
            why = head_move_reason(folder, sub, args)
            if why:
                return HEAD_REFUSED + why
            continue
        why = (push_reason(folder, args) if sub == "push" else reset_reason(folder, args, line.own) if sub == "reset"
               else None)
        if why:
            return REFUSED + why
        if sub in ("commit", "push"):  # last: a history rewrite names its own, graver fault first
            why = hooks_reason(folder, sub)
            if why:
                return why
    return feed_reason(expanded, split(expanded) or split(command), raw, event, cwds)
