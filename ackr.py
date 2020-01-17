#!/usr/bin/env python3.6
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

import argparse
import datetime
import json
import urllib.request
import logging
import shlex
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


# Where ackr state will be stored.
ACKR_DIR = Path.home() / ".ackr"

# Used to generate references to tags for your PRs.
ACKR_GH_USER = os.environ.get('ACKR_GH_USER', 'jamesob')

# Your preferred text editor.
EDITOR = os.environ.get('EDITOR', 'nvim')

# The git remote name asociated with the `bitcoin/bitcoin` upstream.
UPSTREAM = os.environ.get('ACKR_UPSTREAM', 'upstream')

if os.environ.get('ACKR_DIR'):
    ACKR_DIR = Path(os.path.expandvars(os.environ['ACKR_DIR']))

BY_DATE_DIR = ACKR_DIR / "by-date"

log = logging.getLogger(__name__)
logging.basicConfig()

DEBUG = False


def _ensure_location():
    if not Path('./src').is_dir() or not Path('./.git').is_dir():
        raise RuntimeError("must be running within the bitcoin git repo")


def _github_api(path: str) -> dict:
    url = f'https://api.github.com{path}'
    resp = urllib.request.urlopen(url)
    return json.loads(resp.read().decode())


def _fetch_upstream():
    run(shlex.split(f"git fetch {UPSTREAM}")).check_returncode()


def _sh(cmd: str, check: bool = False) -> str:
    """Run a command and return its stdout."""
    return run(cmd, shell=True, stdout=PIPE, check=check).stdout.decode().strip()


class PRData(NamedTuple):
    """Structuured data from a particular pull request."""
    num: int
    author: str
    json_data: dict
    hr_id: str
    ackr_path: Path

    @classmethod
    def from_json_dict(cls, d: dict) -> 'PRData':
        author = d['user']['login']
        hr_id = re.sub(
            r'[^a-zA-Z0-9]+', '_', d['title'].lower())[:24].strip('_')

        return cls(
            num=d['number'],
            json_data=d,
            author=author,
            hr_id=hr_id,
            ackr_path=(ACKR_DIR / "{}.{}.{}".format(
                d['number'], author, hr_id)),
        )

    def existing_tips(self) -> t.Mapping[str, int]:
        """Return the tips we've already seen from this PR."""
        sha_to_seq = {}

        for path in self.ackr_path.glob("[0-9]*.*"):
            seq = path.name.split('.')[0]
            try:
                tipsha = (path / 'HEAD').read_text()
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
        earliest_commit_sha = _sh(
            "git log --no-color {}/master..{} --oneline".format(
                UPSTREAM, ref)).splitlines()[-1].split()[0]
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
                prdata.num, seq, prdata.author, prdata.hr_id),
            ackr_seq=seq,
            ackr_path=ackr_path,
        )


def pull(prnum: int):
    """
    Given a PR number, retrieve the code from Github and do a few things:

    - create a corresponding ackr/ tag for the tip,
    - generate a diff relative to the base of the branch and save it,
    - generate a review checklist with all commits.
    """
    _fetch_upstream()
    pr = PRData.from_json_dict(
        _github_api("/repos/bitcoin/bitcoin/pulls/" + str(prnum)))
    pr.ackr_path.mkdir(exist_ok=True)
    tip = TipData.from_prdata(pr)

    if DEBUG:
        print("Latest tip is {}".format(tip.tip_sha))
        print("Existing tips found: {}".format(pr.existing_tips()))

    if tip.tip_sha in pr.existing_tips():
        print("PR up to date ({})".format(tip.tip_sha[:8]))
        return

    tip.ackr_path.mkdir()
    by_date_name = (
        datetime.date.today().strftime('%Y-%m-%d') + '.' + pr.ackr_path.name +
        f'.{tip.ackr_seq}')
    ln_loc = BY_DATE_DIR / by_date_name

    # Create a symlink to populate the by-date directory.
    _sh(f"ln -rs {tip.ackr_path} {ln_loc}")

    _sh("git tag {} {}".format(tip.ackr_tag, tip.tip_sha))
    print("Tagged {} with {}".format(tip.tip_sha, tip.ackr_tag))
    (tip.ackr_path / "pr.json").write_text(json.dumps(pr.json_data, indent=2))
    (tip.ackr_path / "HEAD").write_text(tip.tip_sha)
    (tip.ackr_path / "base.diff").write_text(_sh(
        "git diff {} {}".format(tip.base_sha, tip.tip_sha)))
    (tip.ackr_path / "review-checklist.md").write_text(_sh(
        "git log --no-color --format=oneline --abbrev-commit --no-merges {} "
        "^master | tac | sed -e 's/^/- [ ] /g'".format(tip.tip_sha)
    ))


def print_pr_data(prnum: int):
    """Print the Github API data associated with a PR."""
    pprint.pprint(_github_api("/repos/bitcoin/bitcoin/pulls/" + str(prnum)))


def print_tag_update(tag: str, one, two):
    """Print a message including links to a tagged update for your branch."""
    base = f'https://github.com/{ACKR_GH_USER}/bitcoin/tree/{tag}.'
    print(
        f"[`{tag}.{one}`]({base + one}) -> [`{tag}.{two}`]({base + two})")


def edit_review_notes(tag: t.Optional[str]):
    """Edit the review checklist and notes file for a certain PR revision."""
    rev_dir = _get_current_ackr_dir()
    if not rev_dir:
        sys.exit(1)

    checklist_path = rev_dir / 'review-checklist.md'
    run(f"{EDITOR} {checklist_path}", shell=True)
    print(checklist_path)


def interdiff(tag: str, seq_before=None, seq_after=None):
    """Show the interdiff between two separate PR tips."""


def ack(msg_file: str):
    """Print a signed ACK message and upload it to opentimestamps."""
    head_sha = _sh("git rev-parse HEAD", check=True)
    msg = ''
    ackr_dir = _get_current_ackr_dir()
    msg_path = Path(ackr_dir) / 'ack_message.txt'
    signed_path = Path(ackr_dir) / 'ack_message.asc'
    tag = _get_current_ackr_tag()
    tag_url = f"https://github.com/{ACKR_GH_USER}/bitcoin/tree/{tag}"

    confdata = _parse_configure_log()
    compiler_v = confdata['clang_version'] or confdata['gcc_version']

    header_txt = textwrap.dedent(f"""ACK {head_sha} ([`{ACKR_GH_USER}/{tag}`]({tag_url}))

        <details><summary>Show platform data</summary>
        <p>

        ```
        Tested on {platform.platform()}

        Configured with {confdata['configure_command']}

        Compiled with {confdata['cxx']} {confdata['cxxflags']} i

        Compiler version: {compiler_v}
        ```

        </p></details>

        """)

    if msg_file == '-':
        msg = sys.stdin.read()
    elif not msg_file:
        if not msg_path.is_file():
            msg_path.write_text(header_txt)
        editor = os.environ.get('EDITOR', 'nvim')
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

    signing_key = _sh('git config user.signingkey')

    if not signing_key:
        raise RuntimeError("you need to configure git's user.signingkey")

    signature = None
    try:
        signature = _sh(
            f'gpg -u {signing_key} -o {signed_path} --clearsign {msg_path}',
            check=True)
    except Exception:
        print(f'GPG signing with key {signing_key} failed!', file=sys.stderr)

    out = msg

    if signature:
        out += textwrap.dedent("""
            <details><summary>Show signature data</summary>
            <p>

            ```
            """)
        out += signed_path.read_text()
        out += textwrap.dedent("""
            ```

            </p></details>
            """)

    print()
    print(out)

    if _sh('which xclip'):
        t = pipes.Template()
        t.append('xclip -in -selection clipboard', '--')
        f = t.open('pipefile', 'w')
        f.write(out)
        f.close()

        print()
        print("Signed ACK message copied to clipboard")

    print(f'\nRemember to run\n\n  git push origin {tag}')


def _get_current_ackr_tag() -> str:
    """Get the ackr tag currently associated with the repo's HEAD."""
    tags = _sh(
        'git name-rev --tags --name-only $(git rev-parse HEAD)').split()
    ackr_tags = [t for t in tags if 'ackr/' in t]

    if not ackr_tags:
        print('HEAD not recognized by ackr (tags: {})'.format(tags))
        return None

    return ackr_tags[0]


def _get_current_ackr_dir() -> str:
    """Get the ackr state dir associated with the current revision."""
    tag = _get_current_ackr_tag()

    if not tag:
        return None

    tag = tag.split('ackr/')[-1]
    num, i, author, title = tag.split('.')
    ackr_folder = '{}.{}.{}'.format(num, author, title)
    ackr_dir = ACKR_DIR / ackr_folder

    if not ackr_dir.exists():
        print('No ackr data for {}'.format(ackr_folder))
        return None

    [dirname] = [n for n in ackr_dir.iterdir() if
                 n.name.startswith('{}.'.format(i))]

    return dirname


def _parse_configure_log() -> dict:
    """
    Inspect the config.log file from the bitcoin src dir.
    """
    out = {
        'configure_command': '',
        'clang_version': '',
        'gcc_version': '',
        'cxx': '',
        'cxxflags': '',
    }
    configlog = Path('./config.log')
    if not configlog.is_file():
        print("No config.log found at %s", configlog, file=sys.stderr)
        return {}

    lines = configlog.read_text().splitlines()

    def extract_val(line) -> str:
        return line.split('=', 1)[-1].replace("'", '')

    for line in lines:
        if line.startswith("  $") and 'configure ' in line:
            out['configure_command'] = line.strip('  $')

        elif line.startswith('clang version'):
            out['clang_version'] = line

        elif line.startswith('g++ '):
            out['gcc_version'] = line

        elif line.startswith('CXX='):
            out['cxx'] = extract_val(line)

        elif line.startswith('CXXFLAGS='):
            out['cxxflags'] += extract_val(line)

        elif '_CXXFLAGS=' in line:
            val = extract_val(line)
            if val:
                out['cxxflags'] += val + ' '

    return out



def build_parser():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('-vv', '--verbose', action='store_true')
    subparsers = parser.add_subparsers(dest='cmd')

    tags_parser = subparsers.add_parser('pull', help=pull.__doc__)
    tags_parser.add_argument('pr_num', nargs='?')

    pr_data_p = subparsers.add_parser('prdata', help=print_pr_data.__doc__)
    pr_data_p.add_argument('pr_num', nargs='?')

    tag_update_p = subparsers.add_parser(
        'tagupdate', help=print_tag_update.__doc__)
    tag_update_p.add_argument('tag')
    tag_update_p.add_argument('one')
    tag_update_p.add_argument('two')

    tag_update_p = subparsers.add_parser(
        'review', help=edit_review_notes.__doc__)
    tag_update_p.add_argument(
        'tag', nargs='?', help='defaults to current branch')

    ack_p = subparsers.add_parser(
        'ack', help=ack.__doc__)
    ack_p.add_argument(
        'msg_file', nargs='?',
        help='defaults to creation in editor. pass - for stdin.')

    return parser


if __name__ == '__main__':
    args = build_parser().parse_args()
    _ensure_location()

    if not ACKR_DIR.exists():
        print("Created state directory at {}".format(ACKR_DIR))
        ACKR_DIR.mkdir()

    if not BY_DATE_DIR.exists():
        print("Created link directory at {}".format(BY_DATE_DIR))
        BY_DATE_DIR.mkdir()

    DEBUG = args.verbose

    if args.cmd == 'pull':
        pull(int(args.pr_num))
    elif args.cmd == 'prdata':
        print_pr_data(int(args.pr_num))
    elif args.cmd == 'tagupdate':
        print_tag_update(args.tag, args.one, args.two)
    elif args.cmd == 'review':
        edit_review_notes(args.tag)
    elif args.cmd == 'ack':
        ack(args.msg_file)
    else:
        print("Unrecognized args")
