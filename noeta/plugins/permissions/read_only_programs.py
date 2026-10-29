"""The programs a shell command may run and still only read: each one's
flags and operands (:class:`Spec`), the read-only ``git`` subcommands, the
programs ``xargs`` may run, ``find``'s harmless primaries, and how a
program's words parse against its spec (:func:`parse_flags`)."""

from __future__ import annotations

import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from noeta.toolkit.shell_syntax import Word


@dataclass(frozen=True)
class Spec:
    """What one read-only program accepts. ``flags``: flags taking no value
    (a long one written ``--x=`` also takes an attached ``=value``);
    ``valued``: flags taking a value (the next word, or attached);
    ``path_valued``: of those, the ones whose value is a path. Operands are
    ``paths``, ``text`` or ``pattern`` (text first, then paths, unless a
    ``pattern_flags`` flag gave the pattern). ``numeric``: ``-5`` is a
    count. ``any_flags``: every word is plain text (``echo``).
    ``lead_glob``: no flag of the program writes, runs or reaches outside
    the directory, so an operand may start with a glob (``ls *.py``) though
    it may expand to a name that reads as a flag. ``glob_valued``: flags
    whose value is a file pattern and may be an unquoted glob."""

    flags: frozenset[str] = frozenset()
    valued: frozenset[str] = frozenset()
    path_valued: frozenset[str] = frozenset()
    operands: str = "paths"
    pattern_flags: frozenset[str] = frozenset()
    numeric: bool = False
    any_flags: bool = False
    lead_glob: bool = False
    glob_valued: frozenset[str] = frozenset()
    max_operands: int | None = None
    #: A last look at the parsed flags and operands.
    check: Callable[[list[tuple[str, str | None]], list[Word]], bool] | None = None


def _set(text: str) -> frozenset[str]:
    return frozenset(text.split())


def _spec(flags: str = "", valued: str = "", path_valued: str = "", glob_valued: str = "",
          **kw: object) -> Spec:
    return Spec(flags=_set(flags), valued=_set(valued) | _set(path_valued) | _set(glob_valued),
                 path_valued=_set(path_valued), glob_valued=_set(glob_valued),
                 **kw)  # type: ignore[arg-type]


_GREP = _spec(
    "-i -v -n -c -l -L -h -H -r -E -F -G -P -w -x -o -s -q -z -a -I -b -U -T -Z "
    "--color --color= --colour= --recursive --ignore-case "
    "--no-ignore-case --invert-match --line-number --count --files-with-matches "
    "--files-without-match --no-filename --with-filename --word-regexp --line-regexp "
    "--only-matching --no-messages --quiet --silent --null --null-data --text "
    "--extended-regexp --fixed-strings --basic-regexp --perl-regexp --byte-offset "
    "--initial-tab --binary-files= --label=",
    valued="-e -m -A -B -C --regexp --max-count --after-context --before-context --context",
    glob_valued="--include --exclude --exclude-dir",
    path_valued="-f --file", operands="pattern", pattern_flags=_set("-e --regexp -f --file"),
    numeric=True, lead_glob=True)
_RG = _spec(
    "-i -s -S -v -n -N -c -l -w -x -o -F -P -U -H -I -u -uu -uuu -a -z -0 -p -q -e "
    "--ignore-case --case-sensitive --smart-case --invert-match --line-number "
    "--no-line-number --count --count-matches --files-with-matches --files-without-match "
    "--word-regexp --line-regexp --only-matching --fixed-strings --pcre2 --multiline "
    "--with-filename --no-filename --no-heading --heading --hidden --no-ignore "
    "--no-ignore-vcs --text --null --pretty --quiet --files --json --vimgrep "
    "--column --stats --sort= --sortr= --color= --type-list --trim --unrestricted "
    "--no-messages --max-columns-preview --passthru --crlf --byte-offset",
    valued="-e -t -T -m -A -B -C -d -M -r -j --regexp --type --type-not "
           "--max-count --after-context --before-context --context --max-depth --max-columns "
           "--replace --threads --max-filesize --encoding -E",
    path_valued="-f --file --ignore-file", glob_valued="-g --glob --iglob", operands="pattern",
    pattern_flags=_set("-e --regexp -f --file --files --type-list"))


def _jq_ok(_flags: list[tuple[str, str | None]], operands: list[Word]) -> bool:
    if not operands:
        return True
    return not re.search(r"\$ENV\b|\benv\b|\binput_filename\b|\bimport\b|\binclude\b"
                         r"|\$__loc__|\bdebug\b|\bstderr\b|\binput\b|\binputs\b",
                         operands[0].text)


def _ls_ok(flags: list[tuple[str, str | None]], _operands: list[Word]) -> bool:
    names = {name for name, _v in flags}
    return not (names & {"-L", "--dereference"} and names & {"-R", "--recursive"})


def _date_ok(_flags: list[tuple[str, str | None]], operands: list[Word]) -> bool:
    return all(w.text.startswith("+") for w in operands)


#: Programs that only read (or print), with the flags each accepts. A
#: program missing here, or a flag missing from its list, is not read-only.
#: Left out on purpose: ``env``, ``command``, ``nice``, ``xargs`` (they run
#: another program; ``xargs`` is judged with its program below), ``less`` /
#: ``more`` (run commands), ``yq`` (``-i``), ``printenv`` (prints secrets),
#: ``awk`` / ``sed`` (run programs, write files), and the flags that make a
#: recursion follow every link it meets (``grep -R``, ``rg -L``, ``find -L``,
#: ``du -L``, ``tree -l``, ``ls -LR``): only the paths written on the command
#: line are checked for where they lead.
READ_ONLY: dict[str, Spec] = {
    "cat": _spec("-A -b -e -E -n -s -t -T -u -v --show-all --number --number-nonblank "
                 "--squeeze-blank --show-ends --show-tabs --show-nonprinting", lead_glob=True),
    "head": _spec("-q -v -z --quiet --silent --verbose --zero-terminated",
                  valued="-n -c --lines --bytes", numeric=True, lead_glob=True),
    "tail": _spec("-q -v -z -r --quiet --silent --verbose --zero-terminated",
                  valued="-n -c --lines --bytes", numeric=True, lead_glob=True),
    "wc": _spec("-c -m -l -L -w --bytes --chars --lines --words --max-line-length",
                lead_glob=True),
    "ls": _spec(" ".join(f"-{c}" for c in "aAbBcCdfFgGhHiklLmnNopqQrRsStuUvxX1Z") +
                " --all --almost-all --human-readable --si --classify --color --color= "
                "--group-directories-first --full-time --time-style= --sort= --time= "
                "--reverse --recursive --size --inode --directory --dereference --no-group "
                "--numeric-uid-gid --indicator-style= --format= --quoting-style= "
                "--literal --escape --hide= --ignore= --ignore-backups --width=",
                valued="-I -w -T --ignore --hide --block-size", lead_glob=True,
                check=_ls_ok),
    "pwd": _spec("-L -P", operands="none"),
    "echo": _spec(any_flags=True, operands="text"),
    "printf": _spec(any_flags=True, operands="text"),
    "true": _spec(any_flags=True, operands="text"),
    "false": _spec(any_flags=True, operands="text"),
    "test": _spec(any_flags=True, operands="text"),
    "[": _spec(any_flags=True, operands="text"),
    "which": _spec("-a -s", operands="text"),
    "type": _spec("-a -t -p -P -f", operands="text"),
    "file": _spec("-b -i -L -h -z -k -N -0 -r --brief --mime --mime-type --mime-encoding "
                  "--dereference --no-dereference --uncompress --keep-going --raw"),
    "stat": _spec("-L -t -f --dereference --terse --file-system",
                  valued="-c --format --printf", lead_glob=True),
    "du": _spec("-a -b -c -h -k -m -s -x -H -l -0 --all --bytes --total --human-readable "
                "--summarize --apparent-size --one-file-system --dereference-args --si "
                "--max-depth= --threshold= --time",
                valued="-d -t -B --max-depth --exclude --block-size", lead_glob=True),
    "df": _spec("-a -h -H -i -k -l -P -T --all --human-readable --si --inodes --local "
                "--portability --print-type --total --output="),
    "tree": _spec("-a -d -f -i -s -h -D -C -n -F -p -u -g -q -N -r -t -v -U -x -J -X "
                  "--noreport --dirsfirst --du --prune --gitignore --charset= --sort=",
                  valued="-L -I -P --filelimit"),
    "grep": _GREP, "egrep": _GREP, "fgrep": _GREP,
    "rg": _RG,
    "sort": _spec("-b -d -f -g -i -M -h -n -R -r -V -c -C -m -s -u -z --ignore-leading-blanks "
                  "--dictionary-order --ignore-case --general-numeric-sort --human-numeric-sort "
                  "--month-sort --numeric-sort --random-sort --reverse --version-sort --check "
                  "--merge --stable --unique --zero-terminated",
                  valued="-k -t -S --key --field-separator --buffer-size --parallel"),
    "uniq": _spec("-c -d -D -u -i -z --count --repeated --unique --ignore-case "
                  "--zero-terminated --all-repeated= --group=",
                  valued="-f -s -w --skip-fields --skip-chars --check-chars", max_operands=1),
    "cut": _spec("-s -z -n --complement --only-delimited --zero-terminated",
                 valued="-b -c -f -d --bytes --characters --fields --delimiter "
                        "--output-delimiter"),
    "tr": _spec("-c -C -d -s -t --complement --delete --squeeze-repeats --truncate-set1",
                operands="text"),
    "nl": _spec("-p", valued="-b -n -w -s -v -i"),
    "diff": _spec("-u -c -q -r -N -i -w -b -B -a -y -s -t -p -E -Z --brief --recursive "
                  "--new-file --ignore-case --ignore-all-space --ignore-space-change "
                  "--ignore-blank-lines --text --side-by-side --report-identical-files "
                  "--expand-tabs --show-c-function --unified --unified= --context= "
                  "--color --color= --minimal --strip-trailing-cr --suppress-common-lines",
                  valued="-U -C -W -x --exclude --width"),
    "cmp": _spec("-b -l -s --print-bytes --verbose --quiet --silent", valued="-i -n"),
    "comm": _spec("-1 -2 -3 -i -z --check-order --nocheck-order --total",
                  valued="--output-delimiter"),
    "basename": _spec("-a -z --multiple --zero", valued="-s --suffix", operands="text"),
    "dirname": _spec("-z --zero", operands="text"),
    "realpath": _spec("-e -m -L -P -q -s -z --canonicalize-existing --canonicalize-missing "
                      "--logical --physical --quiet --strip --no-symlinks --zero",
                      valued="--relative-to --relative-base"),
    "readlink": _spec("-f -e -m -n -q -s -v -z --canonicalize --canonicalize-existing "
                      "--canonicalize-missing --no-newline --quiet --silent --verbose --zero"),
    "date": _spec("-u -R --utc --universal --rfc-email --iso-8601 --iso-8601= --rfc-3339=",
                  valued="-d --date", path_valued="-r --reference", operands="text",
                  check=_date_ok),
    "whoami": _spec(operands="none"),
    "id": _spec("-u -g -G -n -r -a --user --group --groups --name --real", operands="text"),
    "uname": _spec("-a -s -n -r -v -m -p -i -o --all --kernel-name --nodename "
                   "--kernel-release --kernel-version --machine --processor "
                   "--hardware-platform --operating-system", operands="none"),
    "hostname": _spec("-f -s -i -I -d -A --fqdn --short --ip-address --all-ip-addresses "
                      "--domain", operands="none"),
    "sleep": _spec(operands="text"),
    "seq": _spec("-w --equal-width", valued="-s -f --separator --format", operands="text"),
    **{name: _spec("-b -t -c -z --binary --text --check --tag --zero --quiet --status --strict "
                   "--warn --ignore-missing", valued="-a --algorithm" if name == "shasum" else "")
       for name in ("md5sum", "sha1sum", "sha256sum", "sha512sum", "shasum")},
    "column": _spec("-t -x -n -e -L -R --table --fillrows", valued="-s -c -o -N -W --separator "
                    "--output-separator --output-width --table-columns"),
    "strings": _spec("-a -d -f -w --all --data --print-file-name --include-all-whitespace",
                     valued="-n -t -e --bytes --radix --encoding", numeric=True),
    "hexdump": _spec("-b -c -C -d -o -x -v --canonical", valued="-n -s --length --skip"),
    "xxd": _spec("-a -b -c -e -E -i -p -u -C --autoskip --bits --ps --plain --uppercase",
                 valued="-l -s -g -o -n --len --seek --groupsize --offset --name",
                 max_operands=1),
    "od": _spec("-a -b -c -d -f -i -l -o -s -v -x --output-duplicates",
                valued="-A -j -N -t -w --address-radix --skip-bytes --read-bytes --format "
                       "--width"),
    "jq": _spec("-r -j -c -n -e -s -S -a -C -M -R --raw-output --join-output --compact-output "
                "--null-input --exit-status --slurp --sort-keys --ascii-output --color-output "
                "--monochrome-output --raw-input --tab --seq --raw-output0",
                valued="--indent", operands="pattern", check=_jq_ok),
    "pgrep": _spec("-f -l -a -x -i -n -o -c -v -w --full --list-name --list-full --exact "
                   "--ignore-case --newest --oldest --count --inverse",
                   valued="-u -U -g -G -P -s -t --euid --uid --group --parent --session",
                   operands="text"),
    "ps": _spec(any_flags=True, operands="text"),
    "uptime": _spec("-p -s --pretty --since", operands="none"),
    "free": _spec("-b -k -m -g -h -t -w -l --bytes --kilo --mega --giga --human --total "
                  "--wide --lohi --si", operands="none"),
    "nproc": _spec("--all", operands="none"),
}

# ---------------------------------------------------------------- git

#: Flags every read-only ``git`` subcommand below takes (from the log, diff
#: and show families); a subcommand adds its own.
_GIT_SHOW_FLAGS = (
    "--oneline --stat --stat= --shortstat --numstat --name-only --name-status --graph "
    "--decorate --decorate= --no-decorate --all --branches --branches= --tags --tags= "
    "--remotes --remotes= --no-merges --merges --first-parent --reverse --patch -p -u -s "
    "--no-patch --format= --pretty --pretty= --abbrev-commit --no-abbrev-commit --abbrev "
    "--abbrev= --date= --relative-date --since= --until= --after= --before= --author= "
    "--committer= --grep= -i --regexp-ignore-case --all-match --invert-grep --max-count= "
    "--skip= --follow --cached --staged --no-color --color --color= --word-diff "
    "--word-diff= --color-words --ignore-all-space -w -b --ignore-space-change "
    "--ignore-blank-lines --unified= --diff-filter= --find-renames --find-renames= -M -C "
    "--summary --raw --check --dirstat --dirstat= --left-right --cherry-pick --cherry-mark "
    "--boundary --topo-order --date-order --author-date-order --no-walk --no-ext-diff "
    "--no-textconv --minimal --patience --histogram --exit-code --quiet -q -z --full-index "
    "--binary --src-prefix= --dst-prefix= --no-prefix --relative --relative= --merge-base "
    "--source --parents --children --walk-reflogs -g --compact-summary --full-diff "
    "--simplify-by-decoration --ancestry-path -m --diff-merges= --no-renames --name-status "
    "--full-history --log-size --encoding= --expand-tabs --no-expand-tabs --notes "
    "--no-notes --show-notes --mailmap --use-mailmap --no-mailmap --remerge-diff -R "
    "--irreversible-delete -D --function-context -W --ita-invisible-in-index --pickaxe-all "
    "--pickaxe-regex --stdin")
_GIT_SHOW_VALUED = "-n -U -S -G -L --max-count --skip --since --until --author --grep"
GIT_SUBS: dict[str, Spec] = {}
for _name in ("log", "show", "diff", "whatchanged", "shortlog"):
    GIT_SUBS[_name] = _spec(_GIT_SHOW_FLAGS + (" -e --email --summary --numbered"
                                                if _name == "shortlog" else ""),
                            valued=_GIT_SHOW_VALUED, operands="text",
                            numeric=True)
GIT_SUBS.update({
    "status": _spec("-s -b -v -z --short --branch --long --verbose --show-stash "
                    "--ahead-behind --no-ahead-behind --renames --no-renames --porcelain "
                    "--porcelain= --untracked-files= --ignored --ignored= --column --no-column "
                    "-u -uno -uall -unormal", operands="text"),
    "ls-files": _spec("-c -d -m -o -i -s -u -k -z -t -v -f --cached --deleted --modified "
                      "--others --ignored --stage --unmerged --killed --exclude-standard "
                      "--full-name --error-unmatch --directory --no-empty-directory --eol "
                      "--abbrev --abbrev= --recurse-submodules --deduplicate --format= "
                      "--exclude=", valued="-x --exclude", operands="text"),
    "ls-tree": _spec("-r -t -d -l -z --name-only --name-status --object-only --full-name "
                     "--full-tree --abbrev --abbrev= --format= --long", operands="text"),
    "blame": _spec("-l -t -s -e -w -n -f -p -c -b --porcelain --line-porcelain --show-email "
                   "--show-name --show-number --root --show-stats --incremental --date= "
                   "--abbrev= --color-lines --color-by-age --minimal --progress "
                   "--no-progress -M -C", valued="-L --since -S", operands="text"),
    "rev-parse": _spec("--show-toplevel --abbrev-ref --abbrev-ref= --short --short= --verify "
                       "-q --quiet --git-dir --is-inside-work-tree --is-inside-git-dir "
                       "--show-prefix --show-cdup --symbolic --symbolic-full-name "
                       "--git-common-dir --absolute-git-dir --is-bare-repository "
                       "--is-shallow-repository --all --branches --tags --remotes "
                       "--show-superproject-working-tree --show-object-format "
                       "--show-object-format= --sq-quote --local-env-vars --default",
                       operands="text"),
    "cat-file": _spec("-t -s -p -e --batch-check --batch --allow-unknown-type --follow-symlinks"
                      " --batch-all-objects --unordered", operands="text"),
    "describe": _spec("--tags --always --long --abbrev= --dirty --dirty= --broken --all "
                      "--contains --exact-match --first-parent --match= --exclude= "
                      "--candidates=", operands="text"),
    "grep": _spec("-n -i -v -w -l -L -c -h -H -E -F -G -P -I -o -q -z -r -a "
                  "--cached --untracked --heading --break --name-only "
                  "--files-with-matches --files-without-match --count --all-match "
                  "--only-matching --line-number --column --ignore-case --word-regexp "
                  "--fixed-strings --extended-regexp --basic-regexp --perl-regexp --and "
                  "--or --not --full-name --recurse-submodules --show-function -p "
                  "--function-context -W --null --text --no-textconv --quiet "
                  "--invert-match --max-depth= --color --color= --no-color --threads=",
                  valued="-e -A -B -C -m --max-count --after-context --before-context "
                         "--context --max-depth", path_valued="-f", operands="text",
                  numeric=True),
    "reflog": _spec(_GIT_SHOW_FLAGS, valued=_GIT_SHOW_VALUED,
                    operands="text", numeric=True),
    "merge-base": _spec("--is-ancestor --all --octopus --fork-point --independent -a",
                        operands="text"),
    "name-rev": _spec("--name-only --tags --all --always --no-undefined --refs= "
                      "--exclude=", operands="text"),
    "count-objects": _spec("-v -H --verbose --human-readable", operands="none"),
    "branch": _spec("-a -r -v -vv -l --list --all --remotes --verbose --show-current "
                    "--contains --contains= --no-contains= --merged --merged= --no-merged "
                    "--no-merged= --sort= --format= --points-at= --color --color= --no-color "
                    "--column --no-column --ignore-case --abbrev= --no-abbrev",
                    operands="text"),
    "tag": _spec("-l --list -n --sort= --contains --contains= --no-contains= --merged "
                 "--merged= --no-merged --no-merged= --points-at= --format= --color "
                 "--color= --ignore-case --column --no-column", operands="text",
                 numeric=True),
    "remote": _spec("-v --verbose", operands="text"),
    "config": _spec("--get --get-all --get-regexp --list -l --global --local --system "
                    "--worktree --show-origin --show-scope --null -z --name-only --type= "
                    "--bool --int --path --includes --no-includes --default=",
                    operands="text"),
    "stash": _spec(_GIT_SHOW_FLAGS, valued=_GIT_SHOW_VALUED,
                   operands="text", numeric=True),
})
#: Global ``git`` options a read-only command may carry before its
#: subcommand (``-C`` takes a directory). Not ``-c`` / ``--config-env`` /
#: ``--exec-path`` / ``--git-dir`` / ``-p`` (a configured pager).
GIT_GLOBAL = frozenset({"--no-pager", "-P", "--no-optional-locks", "--literal-pathspecs",
                         "--no-replace-objects", "--glob-pathspecs", "--noglob-pathspecs",
                         "--icase-pathspecs", "--no-advice", "--bare"})


def git_operands_ok(sub: str, flags: list[tuple[str, str | None]],
                    operands: list[Word]) -> bool:
    names = {f for f, _v in flags}
    words = [w.text for w in operands]
    if sub in ("branch", "tag"):
        # An operand without ``--list`` creates a branch or a tag.
        return not words or bool(names & {"-l", "--list"})
    if sub == "remote":
        return not words or (words[0] == "get-url" and len(words) <= 2)
    if sub == "config":
        return bool(names & {"--get", "--get-all", "--get-regexp", "--list", "-l"})
    if sub == "stash":
        return words[:1] in (["list"], ["show"])
    if sub == "reflog":
        return not words or words[0] not in ("expire", "delete", "drop")
    return True


# ---------------------------------------------------------------- parsing flags


#: A glob whose expansion, read as a flag, could only name a program by a
#: file extension (``*.py``): no ``/`` and a literal ``.ext`` at the end.
_EXTENSION_GLOB = re.compile(r"[^/]*[*?\]]\.[\w.-]+")


def _glob_value_ok(spec: Spec, word: Word) -> bool:
    """``word`` (a :attr:`Spec.glob_valued` flag's separate value) may
    stay an unquoted glob: whatever it expands to stays a harmless value
    or an operand inside the directory."""
    if word.expands or word.brace or word.tilde:
        return False
    return word.plain or not word.lead_glob or spec.lead_glob or bool(
        _EXTENSION_GLOB.fullmatch(word.text))


def parse_flags(spec: Spec, words: Sequence[Word]
                ) -> tuple[list[tuple[str, str | None]], list[Word]] | None:
    """``(flags with their values, operands)`` when every flag is on the
    spec's list; ``None`` otherwise."""
    flags: list[tuple[str, str | None]] = []
    operands: list[Word] = []
    i, ended = 0, False
    while i < len(words):
        word = words[i]
        a = word.text
        if ended or spec.any_flags or a == "-" or not a.startswith("-"):
            operands.append(word)
            i += 1
            continue
        if not word.plain:
            # ``--include=*.py``: whatever it expands to keeps its flag.
            name, eq, value = a.partition("=")
            if not eq or name not in spec.glob_valued or word.expands or word.brace or (
                    re.search(r"[*?\[]", name)):
                return None
            flags.append((name, value))
            i += 1
            continue
        if a == "--":
            ended = True
            i += 1
            continue
        if spec.numeric and re.fullmatch(r"-\d+", a):
            flags.append((a, None))
            i += 1
            continue
        if a.startswith("--"):
            name, eq, value = a.partition("=")
            if name in spec.valued:
                if not eq:
                    i += 1
                    if i >= len(words) or not (words[i].plain or (
                            name in spec.glob_valued and _glob_value_ok(spec, words[i]))):
                        return None
                    value = words[i].text
                flags.append((name, value))
            elif (eq and f"{name}=" in spec.flags) or (not eq and (
                    name in spec.flags or f"{name}=" in spec.flags)):
                flags.append((name, value if eq else None))
            else:
                return None
            i += 1
            continue
        if a in spec.flags:  # a multi-letter short flag (``-uno``, ``-vv``)
            flags.append((a, None))
            i += 1
            continue
        j = 1
        while j < len(a):
            flag = f"-{a[j]}"
            if flag in spec.valued:
                value = a[j + 1:]
                if not value:
                    i += 1
                    if i >= len(words) or not (words[i].plain or (
                            flag in spec.glob_valued and _glob_value_ok(spec, words[i]))):
                        return None
                    value = words[i].text
                flags.append((flag, value))
                break
            if flag not in spec.flags:
                return None
            flags.append((flag, None))
            j += 1
        i += 1
    return flags, operands


# ---------------------------------------------------------------- xargs and find

#: Programs ``xargs`` may run in a read-only command: whatever words its
#: input adds, they cannot write a file or run a program.
XARGS_TARGETS = frozenset({"wc", "cat", "head", "tail", "grep", "egrep", "fgrep", "ls",
                           "file", "stat", "du", "md5sum", "sha1sum", "sha256sum"})
XARGS_FLAGS = frozenset({"-0", "-r", "-t", "--null", "--no-run-if-empty", "--verbose"})
XARGS_VALUED = frozenset({"-n", "-L", "-d", "-s", "-P"})
XARGS_LONG = ("--max-args=", "--max-lines=", "--delimiter=", "--max-chars=", "--max-procs=")

#: ``find`` primaries that only test or print; those taking a value, and
#: of those the ones whose value is a path. Anything else (``-exec``,
#: ``-delete``, ``-fprint``, ``-files0-from`` ...) is not read-only.
FIND_FLAGS = frozenset("""-print -print0 -prune -o -a -or -and -not ! ( ) , -empty -readable
    -writable -executable -depth -xdev -mount -true -false -ls -nouser -nogroup -quit
    -noleaf -ignore_readdir_race -daystart""".split())
FIND_VALUED = frozenset("""-name -iname -path -ipath -wholename -iwholename -regex -iregex
    -type -xtype -maxdepth -mindepth -size -mtime -mmin -atime -amin -ctime -cmin -user -group
    -uid -gid -perm -links -inum -regextype -printf -used -lname -ilname""".split())
FIND_PATH_VALUED = frozenset({"-newer", "-anewer", "-cnewer", "-samefile"})
