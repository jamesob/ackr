# ackr

Workflow tooling for Bitcoin Core review. 

Only semi-stable, but under active use and development.

---


Ackr helps to maintain a local record of code reviewed, providing systematic
tagging for each revision of each branch reviewed, as well as generating nice
things like commit checklists.

Ackr stores PR metadata somewhere (~/.ackr by default) and allows, e.g., easy
interdiff generation.

``` sh
% tree -L 2 ~/.ackr
/home/james/.ackr
├── 18722.vasild.addrman_improve_performa
│   └── 1.1e1cc9d
├── 18921.fanquake.build_add_stack_clash_an
│   └── 1.b536813
├── 19160.ryanofsky.multiprocess_add_basic_s
│   ├── 1.36f1fbf
│   ├── 2.6a2951a
│   └── 3.1290ccf
├── 19953.sipa.implement_bip_340_342_va
│   └── 1.0e2a5e4
├── 21009.dhruv.remove_rewindblockindex
│   └── 1.6448277
└── by-date
    ├── 2020-08-28.18921.fanquake.build_add_stack_clash_an.1 -> ../18921.fanquake.build_add_stack_clash_an/1.b536813
    ├── 2020-09-01.18722.vasild.addrman_improve_performa.1 -> ../18722.vasild.addrman_improve_performa/1.1e1cc9d
    ├── 2020-10-14.19953.sipa.implement_bip_340_342_va.1 -> ../19953.sipa.implement_bip_340_342_va/1.0e2a5e4
    ├── 2021-03-15.19160.ryanofsky.multiprocess_add_basic_s.1 -> ../19160.ryanofsky.multiprocess_add_basic_s/1.36f1fbf
    ├── 2021-03-22.19160.ryanofsky.multiprocess_add_basic_s.2 -> ../19160.ryanofsky.multiprocess_add_basic_s/2.6a2951a
    ├── 2021-03-26.21009.dhruv.remove_rewindblockindex.1 -> ../21009.dhruv.remove_rewindblockindex/1.6448277
    └── 2021-03-31.19160.ryanofsky.multiprocess_add_basic_s.3 -> ../19160.ryanofsky.multiprocess_add_basic_s/3.1290ccf

20 directories, 0 files
```


``` sh
% tree -L 2 ~/.ackr/19160.ryanofsky.multiprocess_add_basic_s/1.36f1fbf
/home/james/.ackr/19160.ryanofsky.multiprocess_add_basic_s/1.36f1fbf
├── base.diff
├── HEAD
├── pr.json
└── review-checklist.md

0 directories, 4 files
```

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

Ackr can also do cool things like generate GPG-signed ACKs with information
about your test environment by parsing config.log (`ackr ack --help`).

## Installation

Requires Python 3.7+.

``` sh
git clone https://github.com/jamesob/ackr.git
cd ackr
pip install clii  # or obtain from github.com/jamesob/clii
pip install -e .
```

## Configuration

You can either configure ackr through a JSON file placed at `~/.config/ackr`
or via environment variables. 

| Config file name | Environment variable name | Description  | Default |
| ---------------- | ------------------------- | ----------- | -------- | 
| `storage_dir` | `ACKR_DIR` | Where ackr data (tag information, etc.) is stored | `~/.ackr` |
| `ghuser` | `ACKR_GH_USER` | Your github username | `jamesob` |
| `upstream_remote_name` | `ACKR_UPSTREAM` | The name of the git remote corresponding to the bitcoin/bitcoin repo | `upstream` |
