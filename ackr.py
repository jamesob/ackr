#!/usr/bin/env python3
# vi: filetype=python sw=4
"""
A small script that assists with reviewing bitcoin/bitcoin PRs.

Ackr helps to maintain a local record of code reviewed, providing systematic
tagging for each revision of each branch reviewed, as well as generating nice
things like commit checklists.

Ackr stores PR metadata somewhere (~/.ackr by default) and allows, e.g., easy
interdiff generation.

It generates git tags of the form `ackr/$PR_NUMBER.$SEQ_NUMBER.$AUTHOR.$TITLE`
for easy reference.

Each time you run `ackr pull $PR_NUMBER`, it retrieves the latest code
associated with a pull request and

  - git tags it per the format above,
  - saves `$ACKR_DIR/$PR/$REVISION/pr.json`, a snapshot of the PR per
    Github's API,
  - saves `$ACKR_DIR/$PR/$REVISION/HEAD`, the SHA of the HEAD,
  - saves `$ACKR_DIR/$PR/$REVISION/base.diff`, a diff of HEAD against
    the base of the branch.
  - saves `$ACKR_DIR/$PR/$REVISION/review-checklist.md`, a markdown checklist
    of commits to review for the PR,
  - creates a date-ordered symlink to the revision folder in `~/.ackr/by-date`.

TODO:

  - finish `interdiff` command
  - `pull --all` with summary of updates

"""

import datetime
import json
import urllib.request
import logging
import pprint
import typing as t
import re
import contextlib
import sys
import os
import threading
import textwrap
import pipes
import platform
from typing import NamedTuple
from pathlib import Path
import subprocess
from subprocess import run

from clii import App


if sys.version_info < (3, 11):
    print("Needs Python version >= 3.11", file=sys.stderr)
    sys.exit(1)


cli = App(description=__doc__)
cli.add_arg("--verbose", "-v", action="store_true", default=False)


ACKR_CONF_PATH = Path.home() / ".config" / "ackr"


def get_conf(key: str, envkey: str, default: t.Any):
    """
    Return a configuration value first from the environment, then from config file.
    """
    if getattr(get_conf, "__cached_conf", None) is None:
        readconf = {}
        if ACKR_CONF_PATH.exists():
            readconf = json.loads(ACKR_CONF_PATH.read_text())
        setattr(get_conf, '__cached_conf', readconf)

    conffile: dict = getattr(get_conf, '__cached_conf')

    return os.path.expanduser(os.environ.get(envkey, conffile.get(key, default)))


# Where ackr state will be stored.
ACKR_DIR = Path(get_conf("storage_dir", "ACKR_DIR", Path.home() / ".ackr"))

# Used to generate references to tags for your PRs.
ACKR_GH_USER = get_conf("ghuser", "ACKR_GH_USER", "jamesob")

# Your preferred text editor.
EDITOR = os.environ.get("EDITOR", "vim")

# The git remote name asociated with the `bitcoin/bitcoin` upstream.
UPSTREAM = get_conf("upstream_remote_name", "ACKR_UPSTREAM", "upstream")

PAGER = get_conf("pager", "PAGER", "less")

# Symlinks to tags are stored ordered here by date for convenient reference.
BY_DATE_DIR = ACKR_DIR / "by-date"

log = logging.getLogger(__name__)
logging.basicConfig()

# Toggled below with the `-v` flag.
DEBUG = False


# 8 bit Color
###############################################################################
#
# TODO this color stuff was taken from some Github page; track it down and credit
# the authos.


def esc(*codes: t.Union[int, str]) -> str:
    """Produces an ANSI escape code from a list of integers
    :rtype: text_type
    """
    return t_("\x1b[{}m").format(t_(";").join(t_(str(c)) for c in codes))


def t_(b: t.Union[bytes, t.Any]) -> str:
    """ensure text type"""
    if isinstance(b, bytes):
        return b.decode()
    return b


def conn_line(msg: str) -> str:
    return green(bold(" ○  ")) + msg


def make_color(start, end: str) -> t.Callable[[str], str]:
    def color_func(s: str) -> str:
        if not sys.stdout.isatty():
            return s

        # render
        return start + t_(s) + end

    return color_func


FG_END = esc(39)
red = make_color(esc(31), FG_END)
green = make_color(esc(32), FG_END)
yellow = make_color(esc(33), FG_END)
blue = make_color(esc(34), FG_END)
cyan = make_color(esc(36), FG_END)
bold = make_color(esc(1), esc(22))


class OutputStreamer(threading.Thread):
    """
    Allow streaming and capture of output from run processes.

    This mimics the file interface and can be passed to
    subprocess.Popen({stdout,stderr}=...).
    """

    def __init__(
        self, *, is_stdout: bool = True, capture: bool = True, quiet: bool = False
    ):
        super().__init__()
        self.daemon = False
        self.fd_read, self.fd_write = os.pipe()
        self.pipe_reader = os.fdopen(self.fd_read)
        self.start()
        self.capture = capture
        self.lines: list[str] = []
        self.is_stdout = is_stdout
        self.quiet = quiet

    def fileno(self):
        return self.fd_write

    def render_line(self, line) -> str:
        if self.is_stdout:
            return f"    {blue(line)}"
        else:
            return f"    {red(line)}"

    def run(self):
        for line in iter(self.pipe_reader.readline, ""):
            if not self.quiet:
                print(self.render_line(line.rstrip("\n")))
            if self.capture:
                self.lines.append(line)

        self.pipe_reader.close()

    def close(self):
        os.close(self.fd_write)


def _ensure_location():
    if not Path("./src").is_dir() or not Path("./.git").is_dir():
        raise die("must be running within the bitcoin git repo")


def _github_api(path: str) -> dict:
    url = f"https://api.github.com{path}"
    resp = urllib.request.urlopen(url)
    return json.loads(resp.read().decode())


def _fetch_upstream(prnum: int):
    _sh(
        f"git fetch {UPSTREAM} master "
        f"+refs/pull/{prnum}/head:refs/{UPSTREAM}/pr/{prnum}",
        check=True,
    )


def _sh(cmd: str, check: bool = False, quiet: bool = False) -> str:
    """Run a command and return its stdout."""
    if DEBUG:
        print(f"[cmd] {cmd}", flush=True)

    output = OutputStreamer(quiet=quiet, capture=True)
    kwargs: dict[str, t.Any] = {}
    kwargs["stdout"] = output
    kwargs["stderr"] = output
    kwargs["shell"] = True

    with subprocess.Popen(cmd, **kwargs) as s:
        output.close()
        output.join()
        returncode = s.wait()

        if returncode != 0 and check:
            die(f"command failed: {cmd}")

    return "".join(output.lines).strip()


def _sh_check(cmd: str) -> bool:
    """Return True if the command completes successfully."""
    out = run(cmd, shell=True, capture_output=True)
    return out.returncode == 0


class PRData(NamedTuple):
    """Structuured data from a particular pull request."""

    num: int
    author: str
    json_data: dict
    hr_id: str
    ackr_path: Path

    @classmethod
    def from_json_dict(cls, d: dict) -> "PRData":
        author = d["user"]["login"]
        hr_id = re.sub(r"[^a-zA-Z0-9]+", "_", d["title"].lower())[:24].strip("_")
        path = (ACKR_DIR / "{}.{}.{}".format(d["number"], author, hr_id))

        # hr_id may change as authors rename PRs, so reuse that path/hr_id if that's
        # happened.
        if (existing := list(ACKR_DIR.glob(f'{d["number"]}.{author}.*'))):
            path = existing[0]
            hr_id = path.name.split('.')[-1]

        return cls(
            num=d["number"],
            json_data=d,
            author=author,
            hr_id=hr_id,
            ackr_path=path,
        )

    def existing_tips(self) -> t.Mapping[str, int]:
        """Return the tips we've already seen from this PR."""
        sha_to_seq = {}

        for path in self.ackr_path.glob("[0-9]*.*"):
            seq = path.name.split(".")[0]
            try:
                tipsha = (path / "HEAD").read_text()
            except Exception:
                print("!!! unable to read tipsha for {}".format(path))
                continue

            sha_to_seq[tipsha] = int(seq)

        return sha_to_seq


class TipData(NamedTuple):
    """Describes a particular HEAD state for some branch."""

    ref: str
    tip_sha: str
    base_sha: str
    ackr_tag: str
    ackr_seq: int
    ackr_path: Path

    @classmethod
    def from_prdata(cls, prdata: PRData):
        ref = "{}/pr/{}".format(UPSTREAM, prdata.num)
        tip_sha = _sh("git rev-parse {}".format(ref), quiet=True)
        gotlog = _sh(
            "git log --no-color {}/master..{} --oneline".format(UPSTREAM, ref),
            check=True,
            quiet=True,
        )
        earliest_commit_sha = gotlog.splitlines()[-1].split()[0]
        base_sha = _sh("git rev-parse {}~1".format(earliest_commit_sha), quiet=True)
        if len(base_sha) > 40:
            raise die(f"base_sha is fucked: {base_sha[:100]}")

        shortsha = tip_sha[:7]
        preexisting_paths = list(prdata.ackr_path.glob("[0-9]*.*"))
        if DEBUG:
            print("Preexisting PR paths: {}".format(preexisting_paths))

        seq = len(preexisting_paths)
        already_exists = any(str(p).endswith(shortsha) for p in preexisting_paths)

        if not already_exists:
            seq += 1

        ackr_path = prdata.ackr_path / "{}.{}".format(seq, tip_sha[:7])

        return cls(
            ref=ref,
            tip_sha=tip_sha,
            base_sha=base_sha,
            ackr_tag="ackr/{}.{}.{}.{}".format(
                prdata.num, seq, prdata.author, prdata.hr_id
            ),
            ackr_seq=seq,
            ackr_path=ackr_path,
        )

def _commit_ackr_state(commit_msg: str) -> bool:
    """If the ackr data directory is a git repo, push it up to its remote."""
    with contextlib.chdir(ACKR_DIR):
        # If .ackr dir isn't a git repo, return early.
        if not _sh_check("git status"):
            return False

        committed = run(f"git add * && git commit -am '{commit_msg}'", shell=True)
        if committed.returncode != 0:
            print("!! failed to commit to ackr data git repo")
            return False

        print(
            f"Pushing ackr state commit '{green(commit_msg)}'..."
        )
        pushed = run("git push origin master", shell=True)
        if pushed.returncode != 0:
            print("!! failed to push to ackr data git repo")
            return False

        return True


def _pull_ackr_state() -> bool:
    """If the ackr data directory is a git repo, push it up to its remote."""
    with contextlib.chdir(ACKR_DIR):
        # If .ackr dir isn't a git repo, return early.
        if not _sh_check("git status"):
            return False

        committed = run("git pull origin master", shell=True)
        if committed.returncode != 0:
            print("!! failed to pull ackr data git repo")
            return False

        return True

@cli.cmd
def pull(prnum: int):
    """
    Given a PR number, retrieve the code from Github and do a few things:

    - create a corresponding ackr/ tag for the tip,
    - generate a diff relative to the base of the branch and save it,
    - generate a review checklist with all commits.
    """
    _pull_ackr_state()
    _sh("git fetch --all", check=True)
    prnum = prnum or int(_get_current_pr_num())
    (tip, changed) = _pull(prnum)

    if changed:
        print(f"Got new tip: {green(tip.ackr_tag)}")
        print()
        print((tip.ackr_path / "review-checklist.md").read_text())
        print()
        _sh(f"git checkout {tip.ackr_tag}")
        _commit_ackr_state(f'Started review: {_get_current_tag()}')
    else:
        print("PR up to date ({})".format(tip.tip_sha[:8]))


def _pull(prnum: int) -> tuple[TipData, bool]:
    """If a new tag was pulled, return the corresponding tipdata."""
    prnum = prnum or int(_get_current_pr_num())
    _fetch_upstream(prnum)
    pr = PRData.from_json_dict(
        _github_api("/repos/bitcoin/bitcoin/pulls/" + str(prnum))
    )
    pr.ackr_path.mkdir(exist_ok=True)
    tip = TipData.from_prdata(pr)

    def create_tag():
        if not _sh("git tag | grep {}".format(tip.ackr_tag), quiet=True).strip():
            _sh("git tag {} {}".format(tip.ackr_tag, tip.tip_sha), quiet=True)
            print(
                f"Pushing new tag " f"{green(tip.ackr_tag)} ({green(tip.tip_sha)})..."
            )
            _sh("git push --no-verify --tags")

    if DEBUG:
        print("Latest tip is {}".format(tip.tip_sha))
        print("Existing tips found: {}".format(pr.existing_tips()))

    if tip.tip_sha in pr.existing_tips():
        create_tag()
        return (tip, False)

    tip.ackr_path.mkdir()
    by_date_name = (
        datetime.date.today().strftime("%Y-%m-%d")
        + "."
        + pr.ackr_path.name
        + f".{tip.ackr_seq}"
    )
    ln_loc = BY_DATE_DIR / by_date_name

    # Create a symlink to populate the by-date directory.
    _sh(f"ln -rs {tip.ackr_path} {ln_loc}")

    create_tag()
    (tip.ackr_path / "pr.json").write_text(json.dumps(pr.json_data, indent=2))
    (tip.ackr_path / "HEAD").write_text(tip.tip_sha)
    (tip.ackr_path / "base.diff").write_text(
        _sh("GIT_PAGER=cat git diff --no-color {} {}".format(
            tip.base_sha, tip.tip_sha), quiet=True)
    )
    checklist = _sh(
        "git log --no-color --format=oneline --abbrev-commit --no-merges {} "
        "^upstream/master | tac | sed -e 's/^/- [ ] /g'".format(tip.tip_sha),
        quiet=True,
    )
    (tip.ackr_path / "review-checklist.md").write_text(checklist)

    return (tip, True)


@cli.cmd
def print_pr_data(prnum: int):
    """Print the Github API data associated with a PR."""
    pprint.pprint(_github_api("/repos/bitcoin/bitcoin/pulls/" + str(prnum)))


@cli.cmd
def print_tag_update(tag: str, one: str, two: str, verbose: bool = False):
    """Print a message including links to a tagged update for your branch."""
    base = f"https://github.com/{ACKR_GH_USER}/bitcoin/tree/{tag}."
    print(f"[`{tag}.{one}`]({base + one}) -> [`{tag}.{two}`]({base + two})")

    range_diff_url = (
        f"https://github.com/{ACKR_GH_USER}/bitcoin/compare/{tag}.{one}..{tag}.{two}")  # noqa
    print(f"\n[View range diff on GitHub]({range_diff_url})")

    if verbose:
        print(
            f"""
<details><summary>Show range-diff</summary>

```sh
$ git range-diff master {tag}.{one} {tag}.{two}

{_sh(f'git range-diff master {tag}.{one} {tag}.{two}', quiet=True)}
```

</details>
            """
        )


@cli.cmd
def review():
    """
    Edit the review checklist and notes file for a certain PR revision.
    """
    rev_dir = _get_current_rev_dir()
    if not rev_dir:
        die("revdir not detected for HEAD")
    tag = _get_current_tag()

    checklist_path = rev_dir / "review-checklist.md"
    run(f"{EDITOR} {checklist_path}", shell=True)
    print(checklist_path)
    _commit_ackr_state(f'Review progress on {tag}')


def get_branch_commits(branch: str):
    """List branch commits, latest first."""
    branch = branch.split('~', 1)[0]  # nip off foobar~n
    lines = _sh(
        "git log --color=never --oneline "
        f"$(git merge-base {UPSTREAM}/master {branch})..{branch}",
        quiet=True,
    )
    return [i.split()[0] for i in lines.splitlines()]


@cli.cmd
def ls():
    out = _sh("git tag | grep '^ackr/'", quiet=True)
    print(out)


@cli.cmd
def to(pr_num: str):
    _sh("git fetch --all", check=True)
    _pull(int(pr_num))
    tags = _get_versions(pr_num)

    if not tags:
        die(f"no tags found for {pr_num}")

    _sh(f"git checkout {tags[0]}")


@cli.cmd
def start():
    """
    Begin review

    If no prnum given, use the current tag.
    """
    prnum = _get_current_pr_num()
    (tip, changed) = _pull(prnum)

    if changed:
        print(red("Warning: ") + f"this tag is out of date! Latest: {tip.ackr_tag}")

    commits = get_branch_commits(_get_current_ackr_tag())
    marker = _curr_commit_marker()

    if marker.exists() and (commit := marker.read_text()):
        assert commit in commits
        _sh(f'git checkout {commit}')
        return

    marker.write_text(commits[-1])
    _sh(f'git checkout {commits[-1]}')


def _curr_commit_marker():
    dir = Path(_get_current_rev_dir())
    return dir / 'current_commit'


@cli.cmd
def next(move: int = 1):
    """Move to the next commit in the branch."""
    commits = list(reversed(get_branch_commits(_get_current_ackr_tag())))
    marker = _curr_commit_marker()

    if not marker.exists() or not (commit := marker.read_text()):
        return start(_get_current_pr_num())

    try:
        idx = commits.index(commit)
    except ValueError:
        die(f"commit {commit} not in branch history")

    try:
        if (i := idx + move) < 0:
            die("at base commit")
        else:
            to_commit = commits[i]
    except IndexError:
        die(f"index out of range for {commits} (idx: {idx}, move: {move})")

    _sh(f'git checkout {to_commit}')
    marker.write_text(to_commit)


@cli.cmd
def prev():
    return next(-1)


@cli.cmd
def revs():
    """Print the rev dirs for the current tag in descending order."""
    for i in _get_ordered_rev_dirs():
        print(i)


@cli.cmd
def rangediff():
    """
    Show the range-diff between the latest and penultimate tags.
    """
    curr_tag = _get_current_ackr_tag()
    if not curr_tag:
        die("couldn't find current ackr tag")
        return

    versions = _get_versions()
    earlier_versions = versions[versions.index(curr_tag) + 1 :]

    if not earlier_versions:
        die(f"no earlier versions to compare to in {versions}")
        return

    prev_tag = earlier_versions[0]

    input(f"Comparing {curr_tag} to {prev_tag} [enter] ")
    cmd = f"git range-diff {UPSTREAM}/master {prev_tag} {curr_tag}"
    print(cmd)
    run(cmd, shell=True)


@cli.cmd
def interdiff():
    """
    Show the diff between ackr's recorded `base.diff` for this tag and the one
    preceding it.
    """
    [rev, prev_rev, *_] = _get_ordered_rev_dirs()
    input(f"Comparing {prev_rev} to {rev} [enter] ")
    run(f"diff -u {prev_rev}/base.diff {rev}/base.diff | {PAGER}", shell=True)


def _get_versions(pr_num=""):
    """Get ordered tags, latest first."""
    if pr_num:
        prefix = f"ackr/{pr_num}"
    else:
        curr_tag = _get_current_ackr_tag()
        prefix = curr_tag.split(".")[0]

    all_tags = _sh(f'git tag | grep "^{prefix}\."', quiet=True).splitlines()
    return sorted(all_tags, reverse=True)


@cli.cmd
def tags():
    """Print the git-ackr tags for the current PR in descending order."""
    for v in _get_versions():
        print(v)


@cli.cmd
def ack(msg_file: str = ""):
    """
    Print a signed ACK message and upload it to opentimestamps.

    Args:
        msg_file:
    """
    _pull_ackr_state()
    head_sha = _sh("git rev-parse HEAD", quiet=True, check=True)
    msg = ""
    ackr_dir = _get_current_rev_dir()
    msg_path = Path(ackr_dir) / "ack_message.txt"
    signed_path = Path(ackr_dir) / "ack_message.asc"
    tag = _get_current_ackr_tag()
    tag_url = f"https://github.com/{ACKR_GH_USER}/bitcoin/tree/{tag}"

    confdata = _parse_configure_log()
    compiler_v = confdata["clang_version"] or confdata["gcc_version"]

    header_txt = f"ACK {head_sha} ([`{ACKR_GH_USER}/{tag}`]({tag_url}))\n\n"

    if msg_file == "-":
        msg = sys.stdin.read()
    elif not msg_file:
        if not msg_path.is_file():
            msg_path.write_text(header_txt)
        editor = os.environ.get("EDITOR", "nvim")
        run(f"{editor} {msg_path}", shell=True, check=True)
        msg = msg_path.read_text()
    elif Path(msg_file).is_file():
        msg = Path(msg_file).read_text()
    else:
        die(f"bad path given: {msg_file}")

    if f"ACK {head_sha[:6]}" not in msg:
        die("message contains incorrect hash")

    msg_path.write_text(msg)
    print(f"Wrote ACK message to {msg_path}")

    signing_key = _sh("git config user.signingkey", quiet=True)

    if not signing_key:
        die("you need to configure git's user.signingkey")

    signed = True
    try:
        _sh(f"gpg -u {signing_key} -o {signed_path} --clearsign {msg_path}", check=True)
    except Exception:
        print(f"GPG signing with key {signing_key} failed!", file=sys.stderr)
        signed = False

    out = msg

    if signed:
        out += textwrap.dedent(
            """
            <details><summary>Show signature data</summary>
            <p>

            ```
            """
        )
        out += signed_path.read_text()
        out += f"""
```

</p></details>

<details><summary>Show platform data</summary>
<p>

```
Tested on {platform.platform()}

Configured with {confdata['configure_command']}

Compiled with {confdata['cxx']} {confdata['cxxflags']} i

Compiler version: {compiler_v}
```

</p></details>

"""

    print()

    print("-" * 80)
    print(out)
    print("-" * 80)

    if _sh("which wl-copy", quiet=True):
        t = pipes.Template()
        t.append("wl-copy", "--")
        f = t.open("pipefile", "w")
        f.write(out)
        f.close()

        print()
        print("Signed ACK message copied to clipboard")

    print(f"\nRunning git push origin {tag}")
    _sh(f"git push --no-verify origin {tag}")
    _commit_ackr_state(f"ACK: {_get_current_tag()}")


def _get_current_ackr_tag() -> str:
    """Get the ackr tag currently associated with the repo's HEAD."""
    tags = _sh(
        "git name-rev --tags --name-only $(git rev-parse HEAD)", quiet=True
    ).split()
    ackr_tags = [t for t in tags if "ackr/" in t]

    if not ackr_tags:
        die("HEAD not recognized by ackr (tags: {})".format(tags))

    assert ackr_tags[0]
    return ackr_tags[0]


def _get_current_pr_num() -> str:
    tag = _get_current_ackr_tag().split("ackr/")[-1]
    num, *_ = tag.split(".")
    return num


def _get_current_tag() -> str:
    """E.g. 28008.1.sipa.bip324_ciphersuite"""
    return _get_current_ackr_tag().split("ackr/")[-1]


def _get_current_tag_data() -> t.List[str]:
    return _get_current_tag().split(".")


def _get_current_rev_dir() -> Path:
    """Get the ackr state dir associated with the current revision."""
    num, i, *_ = _get_current_tag_data()
    [pr_dir] = [n for n in ACKR_DIR.iterdir() if n.name.startswith("{}.".format(num))]
    [rev_dir] = [n for n in pr_dir.iterdir() if n.name.startswith("{}.".format(i))]

    return rev_dir


def _get_ordered_rev_dirs(num: t.Optional[str] = None) -> list[Path]:
    """Get the ackr state dir associated with the current revision."""
    num = num or _get_current_pr_num()
    [pr_dir] = [n for n in ACKR_DIR.iterdir() if n.name.startswith("{}.".format(num))]
    return list(sorted(
        [i for i in pr_dir.iterdir() if re.match(r'\d+\.', i.name)], 
        reverse=True, key=str))


def _parse_configure_log() -> dict:
    """
    Inspect the config.log file from the bitcoin src dir.
    """
    out = {
        "configure_command": "",
        "clang_version": "",
        "gcc_version": "",
        "cxx": "",
        "cxxflags": "",
    }
    configlog = Path("./config.log")
    if not configlog.is_file():
        print("No config.log found at %s", configlog, file=sys.stderr)
        return {}

    lines = configlog.read_text().splitlines()

    def extract_val(line) -> str:
        return line.split("=", 1)[-1].replace("'", "")

    for line in lines:
        if line.startswith("  $") and "configure " in line:
            out["configure_command"] = line.strip("  $")

        elif "clang version" in line:
            out["clang_version"] = line

        elif line.startswith("g++ "):
            out["gcc_version"] = line

        elif line.startswith("CXX="):
            out["cxx"] = extract_val(line)

        elif line.startswith("CXXFLAGS="):
            out["cxxflags"] += extract_val(line)

        elif "_CXXFLAGS=" in line:
            val = extract_val(line)
            if val:
                out["cxxflags"] += val + " "

    return out


def die(msg: str):
    print(red(bold(msg)), file=sys.stderr)
    sys.exit(1)


def check_remotes():
    confpath = Path(".git/config")
    if not confpath.exists():
        die("are you in a git repo?")

    conf = confpath.read_text()

    if f'[remote "{UPSTREAM}"]' not in conf:
        die(
            "Missing upstream remote; run "
            f"`git remote add {UPSTREAM} https://github.com/bitcoin/bitcoin.git"
        )


def main():
    _ensure_location()
    cli.parse_for_run()
    global DEBUG
    DEBUG = cli.args.verbose

    if not ACKR_DIR.exists():
        print("Created state directory at {}".format(ACKR_DIR))
        ACKR_DIR.mkdir()

    if not BY_DATE_DIR.exists():
        print("Created link directory at {}".format(BY_DATE_DIR))
        BY_DATE_DIR.mkdir()

    check_remotes()
    cli.run()


if __name__ == "__main__":
    main()
