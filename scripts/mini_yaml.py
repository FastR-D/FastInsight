"""mini_yaml.py — A tiny YAML-subset parser used as a fallback for the roster.

Supports the subset used by colleagues.yaml:
  * mappings (key: value) with indentation-based nesting
  * sequences (- item) of scalars or mappings
  * quoted ("..." / '...') and plain scalars, ints, floats, booleans, null
  * simple flow lists/maps ([] / {})
  * comments (# ...) and blank lines

Does NOT support: anchors, multiline block scalars.
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
        if (s.startswith("[") and s.endswith("]")) or (
            s.startswith("{") and s.endswith("}")
        ):
            return _parse_flow(s)
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


def _split_flow_items(value):
    items = []
    start = 0
    depth = 0
    quote = None
    for index, char in enumerate(value):
        if quote:
            if char == quote:
                quote = None
        elif char in "\"'":
            quote = char
        elif char in "[{":
            depth += 1
        elif char in "]}":
            depth -= 1
        elif char == "," and depth == 0:
            items.append(value[start:index].strip())
            start = index + 1
    tail = value[start:].strip()
    if tail:
        items.append(tail)
    return items


def _parse_flow(value):
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        return [] if not inner else [_Parser._scalar(item) for item in _split_flow_items(inner)]
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1].strip()
        result = {}
        if not inner:
            return result
        for item in _split_flow_items(inner):
            key, rest = _Parser._split_kv(item)
            if not key:
                raise ValueError("invalid flow mapping")
            result[key] = _Parser._scalar(rest)
        return result
    return _Parser._scalar(value)


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
