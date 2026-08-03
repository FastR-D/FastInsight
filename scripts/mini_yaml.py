"""mini_yaml.py — A tiny YAML-subset parser used as a fallback for the roster.

Supports the subset used by colleagues.yaml:
  * mappings (key: value) with indentation-based nesting
  * sequences (- item) of scalars or mappings
  * quoted ("..." / '...') and plain scalars, ints, floats, booleans, null
  * comments (# ...) and blank lines

Does NOT support: flow style ([] / {}), anchors, multiline block scalars.
If PyYAML is installed it is preferred; this is only the zero-dependency fallback.
"""
import re


class _Parser:
    def __init__(self, lines):
        self.lines = lines  # list of (indent, content)
        self.i = 0

    def parse_node(self, min_indent):
        if self.i >= len(self.lines):
            return None
        ind, content = self.lines[self.i]
        if ind < min_indent:
            return None
        if content.startswith("- "):
            return self.parse_list(ind)
        return self.parse_map(ind)

    def parse_map(self, ind):
        m = {}
        while self.i < len(self.lines):
            ci, content = self.lines[self.i]
            if ci < ind or ci > ind:
                break
            if content.startswith("- "):
                break
            key, rest = self._split_kv(content)
            self.i += 1
            m[key] = self._value(rest, ind)
        return m

    def parse_map_item(self, first_key, first_rest, item_indent):
        m = {first_key: self._value(first_rest, item_indent)}
        while self.i < len(self.lines):
            ci, content = self.lines[self.i]
            if ci < item_indent or ci > item_indent:
                break
            if content.startswith("- "):
                break
            key, rest = self._split_kv(content)
            self.i += 1
            m[key] = self._value(rest, item_indent)
        return m

    def parse_list(self, ind):
        lst = []
        while self.i < len(self.lines):
            ci, content = self.lines[self.i]
            if ci != ind:
                break
            if not content.startswith("- "):
                break
            after = content[2:].strip()
            self.i += 1
            if after == "":
                if self.i < len(self.lines) and self.lines[self.i][0] > ind:
                    lst.append(self.parse_node(self.lines[self.i][0]))
                else:
                    lst.append(None)
            elif self._is_kv(after):
                k, r = self._split_kv(after)
                lst.append(self.parse_map_item(k, r, ind + 2))
            else:
                lst.append(self._scalar(after))
        return lst

    def _value(self, rest, ind):
        if rest == "":
            if self.i < len(self.lines) and self.lines[self.i][0] > ind:
                return self.parse_node(self.lines[self.i][0])
            return None
        return self._scalar(rest)

    @staticmethod
    def _is_kv(s):
        if s and s[0] in "\"'":
            return False
        m = re.match(r"^([^:]+):(\s.*)?$", s)
        if not m:
            return False
        key = m.group(1).strip()
        if "/" in key or "@" in key or key.lower() in ("http", "https", "ftp"):
            return False
        return True

    @staticmethod
    def _split_kv(content):
        in_s = None
        for i, c in enumerate(content):
            if in_s:
                if c == in_s:
                    in_s = None
            else:
                if c in "\"'":
                    in_s = c
                elif c == ":":
                    if i + 1 == len(content) or content[i + 1] == " ":
                        return content[:i].strip(), content[i + 1:].strip()
        return content.strip(), ""

    @staticmethod
    def _scalar(s):
        s = s.strip()
        if s == "" or s in ("null", "~"):
            return None
        if (s[0] == '"' and s[-1] == '"') or (s[0] == "'" and s[-1] == "'"):
            return s[1:-1]
        low = s.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        try:
            return int(s)
        except ValueError:
            pass
        try:
            return float(s)
        except ValueError:
            pass
        return s


def _strip_comment(line):
    out = []
    in_s = None
    for c in line:
        if in_s:
            out.append(c)
            if c == in_s:
                in_s = None
        else:
            if c in "\"'":
                in_s = c
                out.append(c)
            elif c == "#":
                break
            else:
                out.append(c)
    return "".join(out).rstrip()


def load(text):
    """Parse a YAML-subset string into Python objects."""
    raw = []
    for line in text.splitlines():
        stripped = _strip_comment(line)
        if stripped.strip() == "":
            continue
        indent = len(line) - len(line.lstrip(" "))
        raw.append((indent, stripped.strip()))
    if not raw:
        return {}
    return _Parser(raw).parse_node(0)
