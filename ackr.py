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
import sys
import os
import textwrap
import pipes
import platform
from typing import NamedTuple
from pathlib import Path
from subprocess import run, PIPE

from clii import App


cli = App(description=__doc__)
cli.add_arg("--verbose", "-v", action="store_true", default=False)


ACKR_CONF_PATH = Path.home() / '.config' / 'ackr'


def get_conf(key: str, envkey: str, default: t.Any):
    """
    Return a configuration value first from the environment, then from config file.
    """
    if getattr(get_conf, '__cached_conf', None) is None:
        readconf = {}
        if ACKR_CONF_PATH.exists():
            readconf = json.loads(ACKR_CONF_PATH.read_text())
        get_conf.__cached_conf = readconf

    conffile: dict = get_conf.__cached_conf

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


def _ensure_location():
    if not Path("./src").is_dir() or not Path("./.git").is_dir():
        raise RuntimeError("must be running within the bitcoin git repo")


def _github_api(path: str) -> dict:
    url = f"https://api.github.com{path}"
    resp = urllib.request.urlopen(url)
    return json.loads(resp.read().decode())


def _fetch_upstream(prnum: int):
    _sh(
        f"git fetch {UPSTREAM} master +refs/pull/{prnum}/head:refs/{UPSTREAM}/pr/{prnum}",
        check=True)


def _sh(cmd: str, check: bool = False) -> str:
    """Run a command and return its stdout."""
    if DEBUG:
        print(f"[cmd] {cmd}", flush=True)
    return run(cmd, shell=True, stdout=PIPE, check=check).stdout.decode().strip()


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

        return cls(
            num=d["number"],
            json_data=d,
            author=author,
            hr_id=hr_id,
            ackr_path=(ACKR_DIR / "{}.{}.{}".format(d["number"], author, hr_id)),
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

    def next_seq(self) -> int:
        """Get the next sequence number for a new tip."""


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
        tip_sha = _sh("git rev-parse {}".format(ref))
        gotlog = _sh(
            "git log --no-color {}/master..{} --oneline".format(UPSTREAM, ref),
            check=True)
        earliest_commit_sha = gotlog.splitlines()[-1] .split()[0]
        base_sha = _sh("git rev-parse {}~1".format(earliest_commit_sha))
        if len(base_sha) > 40:
            raise RuntimeError(f"base_sha is fucked: {base_sha[:100]}")

        preexisting_paths = list(prdata.ackr_path.glob("[0-9]*.*"))
        if DEBUG:
            print("Preexisting PR paths: {}".format(preexisting_paths))

        seq = len(preexisting_paths) + 1
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


@cli.cmd
def pull(prnum: int):
    """
    Given a PR number, retrieve the code from Github and do a few things:

    - create a corresponding ackr/ tag for the tip,
    - generate a diff relative to the base of the branch and save it,
    - generate a review checklist with all commits.
    """
    _sh("git fetch --all", check=True)
    _fetch_upstream(prnum)
    pr = PRData.from_json_dict(
        _github_api("/repos/bitcoin/bitcoin/pulls/" + str(prnum))
    )
    pr.ackr_path.mkdir(exist_ok=True)
    tip = TipData.from_prdata(pr)

    def create_tag():
        if not _sh("git tag | grep {}".format(tip.ackr_tag)).strip():
            _sh("git tag {} {}".format(tip.ackr_tag, tip.tip_sha))
            print("Tagged {} with {}".format(tip.tip_sha, tip.ackr_tag))
            _sh("git push --tags")

    if DEBUG:
        print("Latest tip is {}".format(tip.tip_sha))
        print("Existing tips found: {}".format(pr.existing_tips()))

    if tip.tip_sha in pr.existing_tips():
        print("PR up to date ({})".format(tip.tip_sha[:8]))
        create_tag()
        return

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
        _sh("git diff {} {}".format(tip.base_sha, tip.tip_sha))
    )
    checklist = _sh(
        "git log --no-color --format=oneline --abbrev-commit --no-merges {} "
        "^upstream/master | tac | sed -e 's/^/- [ ] /g'".format(tip.tip_sha)
    )
    (tip.ackr_path / "review-checklist.md").write_text(checklist)

    print()
    print(checklist)
    print()
    _sh(f"git checkout {tip.ackr_tag}")


@cli.cmd
def print_pr_data(prnum: int):
    """Print the Github API data associated with a PR."""
    pprint.pprint(_github_api("/repos/bitcoin/bitcoin/pulls/" + str(prnum)))


@cli.cmd
def print_tag_update(tag: str, one: str, two: str):
    """Print a message including links to a tagged update for your branch."""
    base = f"https://github.com/{ACKR_GH_USER}/bitcoin/tree/{tag}."
    print(f"[`{tag}.{one}`]({base + one}) -> [`{tag}.{two}`]({base + two})")

    print(
        f"""
<details><summary>Show range-diff</summary>

```sh
$ git range-diff master {tag}.{one} {tag}.{two}

{_sh(f'git range-diff master {tag}.{one} {tag}.{two}')}
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

    checklist_path = rev_dir / "review-checklist.md"
    run(f"{EDITOR} {checklist_path}", shell=True)
    print(checklist_path)


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
    earlier_versions = versions[versions.index(curr_tag) + 1:]

    if not earlier_versions:
        die(f"no earlier versions to compare to in {versions}")
        return

    prev_tag = earlier_versions[0]

    input(f"Comparing {curr_tag} to {prev_tag} [enter] ")
    run(f"git range-diff {UPSTREAM}/master {prev_tag} {curr_tag}", shell=True)


@cli.cmd
def interdiff():
    """
    Show the diff between ackr's recorded `base.diff` for this tag and the one preceding it.
    """
    [rev, prev_rev, *_] = _get_ordered_rev_dirs()
    input(f"Comparing {prev_rev} to {rev} [enter] ")
    run(f"diff -u {prev_rev}/base.diff {rev}/base.diff | {PAGER}", shell=True)


def _get_versions():
    curr_tag = _get_current_ackr_tag()
    prefix = curr_tag.split('.')[0]
    all_tags = _sh(f'git tag | grep "^{prefix}\."').splitlines()
    return sorted(all_tags, reverse=True)


@cli.cmd
def tags():
    """Print the git-ackr tags for the current PR in descending order."""
    for v in _get_versions():
        print(v)


@cli.cmd
def ack(msg_file: str = ''):
    """
    Print a signed ACK message and upload it to opentimestamps.

    Args:
        msg_file:
    """
    head_sha = _sh("git rev-parse HEAD", check=True)
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
        raise RuntimeError(f"bad path given: {msg_file}")

    if f"ACK {head_sha[:6]}" not in msg:
        raise RuntimeError("message contains incorrect hash")

    msg_path.write_text(msg)
    print(f"Wrote ACK message to {msg_path}")

    signing_key = _sh("git config user.signingkey")

    if not signing_key:
        raise RuntimeError("you need to configure git's user.signingkey")

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

    if _sh("which xclip"):
        t = pipes.Template()
        t.append("xclip -in -selection clipboard", "--")
        f = t.open("pipefile", "w")
        f.write(out)
        f.close()

        print()
        print("Signed ACK message copied to clipboard")

    print(f"\nRunning git push origin {tag}")
    _sh(f"git push origin {tag}")


def _get_current_ackr_tag() -> str:
    """Get the ackr tag currently associated with the repo's HEAD."""
    tags = _sh("git name-rev --tags --name-only $(git rev-parse HEAD)").split()
    ackr_tags = [t for t in tags if "ackr/" in t]

    if not ackr_tags:
        die("HEAD not recognized by ackr (tags: {})".format(tags))

    assert ackr_tags[0]
    return ackr_tags[0]


def _get_current_pr_num() -> str:
    tag = _get_current_ackr_tag().split("ackr/")[-1]
    num, *_ = tag.split(".")
    return num


def _get_current_tag_data() -> t.Tuple[int, int, str, str]:
    return _get_current_ackr_tag().split('ackr/')[-1].split('.')


def _get_current_rev_dir() -> str:
    """Get the ackr state dir associated with the current revision."""
    num, i, *_ = _get_current_tag_data()
    [pr_dir] = [n for n in ACKR_DIR.iterdir() if n.name.startswith("{}.".format(num))]
    [rev_dir] = [n for n in pr_dir.iterdir() if n.name.startswith("{}.".format(i))]

    return rev_dir


def _get_ordered_rev_dirs(num: t.Optional[str] = None) -> str:
    """Get the ackr state dir associated with the current revision."""
    num = num or _get_current_pr_num()
    [pr_dir] = [n for n in ACKR_DIR.iterdir() if n.name.startswith("{}.".format(num))]
    return list(sorted(pr_dir.iterdir(), reverse=True, key=str))


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
    print(msg, file=sys.stderr)
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
