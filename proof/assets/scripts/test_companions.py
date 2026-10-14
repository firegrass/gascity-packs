"""Related JS/TS tests from a pinned Git tree, without running repository code.

The operator may opt a rig into this pre-claim scope closure. It returns only
existing test files with a static import path to a scoped file. The dispatcher
records those exact paths in the item BEFORE the ordinary collision-checked
claim. This never gives an in-flight worker permission to widen its own scope.
"""
import posixpath
import re
import subprocess

import path_rules

EXTENSIONS = ('.ts', '.tsx', '.mts', '.cts', '.js', '.jsx', '.mjs', '.cjs')
TEST = re.compile(r'^(?:test|tests)/.+\.(?:test|spec)\.(?:[cm]?[jt]s|[jt]sx)$')
# Tokenize without evaluating JS. Comments/templates and ordinary string
# contents cannot manufacture an import edge.
TOKENS = re.compile(r'''//[^\n]*|/\*[\s\S]*?\*/|`(?:\\.|[^`\\])*`|"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|[A-Za-z_$][\w$]*|[^\s]''')


def git(repo, *args, data=None):
    try:
        result = subprocess.run(['git', '-C', str(repo), *args], input=data,
                                capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError('cannot inspect companion tests: ' + str(exc)) from exc
    if result.returncode:
        raise ValueError('cannot inspect companion tests: ' + result.stderr.decode(errors='replace')[-300:])
    return result.stdout


def snapshot(repo, ref='origin/main'):
    """Read only ordinary tracked source blobs; ignore symlinks and worktree edits."""
    sha = git(repo, 'rev-parse', '--verify', ref + '^{commit}').decode().strip()
    entries = []
    for row in git(repo, 'ls-tree', '-rz', sha).split(b'\0'):
        if not row:
            continue
        metadata, name = row.split(b'\t', 1)
        mode, kind, oid = metadata.split()
        path = name.decode()
        if mode not in (b'100644', b'100755') or kind != b'blob' or not path.endswith(EXTENSIONS):
            continue
        entries.append((path, oid))
    if len(entries) > 10000:
        raise ValueError('companion-test source census exceeds 10000 files; keep explicit scope')
    if not entries:
        return sha, {}
    raw = git(repo, 'cat-file', '--batch', data=b''.join(oid + b'\n' for _, oid in entries))
    offset, files = 0, {}
    for path, oid in entries:
        end = raw.index(b'\n', offset)
        got, kind, size = raw[offset:end].split()
        if got != oid or kind != b'blob':
            raise ValueError('companion-test blob read did not match the pinned tree')
        length = int(size)
        content = raw[end + 1:end + 1 + length]
        if len(content) != length:
            raise ValueError('incomplete companion-test blob read')
        files[path] = content.decode(errors='replace')
        offset = end + length + 2
    return sha, files


def imports(path, text, files):
    """A deliberately bounded static import resolver, including TS .js imports."""
    tokens = [match.group() for match in TOKENS.finditer(text)
              if not match.group().startswith(('//', '/*'))]
    specifiers = []
    for index, token in enumerate(tokens[:-1]):
        if token not in ('from', 'require', 'import') or (index and tokens[index - 1] == '.'):
            continue
        cursor = index + 1
        if token in ('require', 'import') and tokens[cursor] == '(':
            cursor += 1
        if cursor >= len(tokens):
            continue
        value = tokens[cursor]
        if value.startswith(('".', "'.")) and '\\' not in value:
            specifiers.append(value[1:-1])
    targets = set()
    for specifier in specifiers:
        target = posixpath.normpath(posixpath.join(posixpath.dirname(path), specifier))
        if target.startswith('../') or target.startswith('/'):
            continue
        candidates = [target]
        stem, extension = posixpath.splitext(target)
        if extension in ('.js', '.jsx', '.mjs', '.cjs'):
            candidates += [stem + suffix for suffix in EXTENSIONS]
        elif not extension:
            candidates += [target + suffix for suffix in EXTENSIONS]
            candidates += [target + '/index' + suffix for suffix in EXTENSIONS]
        targets.update(candidate for candidate in candidates if candidate in files)
    return targets


def related(files, patterns, max_depth=1):
    """Return exact test paths; importing production modules does not authorize them."""
    reached = {path for path in files if any(path_rules.reaches(pattern, path) for pattern in patterns)}
    reverse = {}
    for path, text in files.items():
        for dependency in imports(path, text, files):
            reverse.setdefault(dependency, set()).add(path)
    if type(max_depth) is not int or max_depth not in (1, 2):
        raise ValueError('test_companion_import_depth must be 1 or 2')
    frontier = set(reached)
    for _ in range(max_depth):
        frontier = {dependent for path in frontier for dependent in reverse.get(path, ())} - reached
        reached.update(frontier)
    return sorted(path for path in reached if TEST.fullmatch(path)
                  and not any(char in path for char in '*?[]\\')
                  and not any(path_rules.reaches(pattern, path) for pattern in patterns))


def scope_update(text, fm, dm, repo, files, max_depth=1):
    """Pure edit: the caller either commits it with a claim or restores the text."""
    scopes = dm.write_scopes(type('Record', (), {'fm': fm})())
    if not scopes or any(owner != repo for owner, _ in scopes):
        raise ValueError('test companions require one explicitly scoped repository')
    additions = related(files, [pattern for _, pattern in scopes], max_depth)
    # Reserved/design rules continue to be admission's, even for future repos.
    additions = [path for path in additions if not path_rules.reserved_scope(repo, path)]
    if not additions:
        return text, []
    scope = list(fm['write_scope']) + [repo + ':' + path for path in additions]
    return dm.rewrite_frontmatter_keys(text, {'write_scope': scope}), additions
