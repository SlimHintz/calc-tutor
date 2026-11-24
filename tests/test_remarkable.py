import io
import json
import shlex
import tarfile
import uuid
from pathlib import Path

import pytest
from fastmcp import Client
from PIL import Image
from rmscene import scene_items as si
from rmscene import write_blocks
from rmscene.crdt_sequence import CrdtSequence, CrdtSequenceItem
from rmscene.scene_stream import (AuthorIdsBlock, MigrationInfoBlock, PageInfoBlock,
                                  SceneGroupItemBlock, SceneLineItemBlock, SceneTreeBlock,
                                  TreeNodeBlock)
from rmscene.tagged_block_common import CrdtId, LwwValue

from calc_tutor import remarkable as rm
from calc_tutor.server import mcp

LAYER = CrdtId(0, 11)


def make_rm(strokes: list[list[tuple[float, float]]], first_id: int = 20) -> bytes:
    """A minimal v6 page: one layer holding a fineliner stroke per list of points."""
    blocks = [
        AuthorIdsBlock(author_uuids={1: uuid.UUID(int=1)}),
        MigrationInfoBlock(migration_id=CrdtId(1, 1), is_device=True),
        PageInfoBlock(loads_count=1, merges_count=0, text_chars_count=0, text_lines_count=0),
        SceneTreeBlock(tree_id=LAYER, node_id=CrdtId(0, 0), is_update=True,
                       parent_id=CrdtId(0, 1)),
        TreeNodeBlock(si.Group(node_id=CrdtId(0, 1))),
        TreeNodeBlock(si.Group(node_id=LAYER, label=LwwValue(CrdtId(0, 12), "Layer 1"))),
        SceneGroupItemBlock(parent_id=CrdtId(0, 1), item=CrdtSequenceItem(
            item_id=CrdtId(0, 13), left_id=CrdtId(0, 0), right_id=CrdtId(0, 0),
            deleted_length=0, value=LAYER)),
    ]
    left = CrdtId(0, 0)
    for n, pts in enumerate(strokes):
        item_id = CrdtId(1, first_id + n)
        line = si.Line(color=si.PenColor.BLACK, tool=si.Pen.FINELINER_2,
                       points=[si.Point(x, y, 0, 0, 24, 0) for x, y in pts],
                       thickness_scale=2.0, starting_length=0.0)
        blocks.append(SceneLineItemBlock(parent_id=LAYER, item=CrdtSequenceItem(
            item_id=item_id, left_id=left, right_id=CrdtId(0, 0), deleted_length=0,
            value=line)))
        left = item_id
    buf = io.BytesIO()
    write_blocks(buf, blocks)
    return buf.getvalue()


class FakeDevice:
    """Stands in for `ssh remarkable <cmd>`: serves stat listings and tar streams from a folder."""

    def __init__(self, root: Path):
        self.root = root
        self.online = True
        self.clock = 1_790_000_000

    def add_doc(self, name: str, pages: list[bytes], file_type: str = "notebook",
                redirs: list[int] | None = None) -> tuple[str, list[str]]:
        doc = str(uuid.uuid4())
        page_ids = [str(uuid.uuid4()) for _ in pages]
        (self.root / f"{doc}.metadata").write_text(json.dumps(
            {"visibleName": name, "type": "DocumentType", "parent": "",
             "lastModified": "1790000000000"}))
        cpages = []
        for i, pid in enumerate(page_ids):
            p = {"id": pid, "idx": {"timestamp": "1:2", "value": f"b{chr(97 + i)}"},
                 "template": {"timestamp": "1:1", "value": "Blank"}}
            if redirs:
                p["redir"] = {"timestamp": "1:2", "value": redirs[i]}
            cpages.append(p)
        (self.root / f"{doc}.content").write_text(json.dumps(
            {"fileType": file_type, "formatVersion": 2, "cPages": {"pages": cpages}}))
        (self.root / doc).mkdir()
        for pid, data in zip(page_ids, pages):
            self.write_page(doc, pid, data)
        return doc, page_ids

    def write_page(self, doc: str, page_id: str, data: bytes) -> None:
        import os
        self.clock += 60
        f = self.root / doc / f"{page_id}.rm"
        f.write_bytes(data)
        os.utime(f, (self.clock, self.clock))

    def __call__(self, cmd: str, timeout: int = 30, data: bytes | None = None) -> bytes:
        if not self.online:
            raise rm.RemarkableError("reMarkable unreachable (asleep or off Wi-Fi?)")
        if cmd.startswith("date +%s"):
            newest = max((int(f.stat().st_mtime) for f in self.root.glob("*/*.rm")), default=0)
            return f"{self.clock}\n{newest}\n".encode()
        if "tar xf -" in cmd:
            with tarfile.open(fileobj=io.BytesIO(data)) as tf:
                tf.extractall(self.root, filter="data")
            self.restarts = getattr(self, "restarts", 0) + ("restart xochitl" in cmd)
            return b""
        if "stat -c" in cmd:
            lines = []
            for f in [*self.root.glob("*.metadata"), *self.root.glob("*.content"),
                      *self.root.glob("*/*.rm")]:
                st = f.stat()
                lines.append(f"{int(st.st_mtime)} {st.st_size} {f.relative_to(self.root)}")
            return "\n".join(lines).encode()
        if "tar cf -" in cmd:
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w") as tf:
                for p in shlex.split(cmd.split("tar cf -", 1)[1]):
                    tf.add(self.root / p, arcname=p)
            return buf.getvalue()
        raise AssertionError(f"unexpected command {cmd}")


@pytest.fixture
def device(tmp_path, monkeypatch):
    root = tmp_path / "device"
    root.mkdir()
    dev = FakeDevice(root)
    monkeypatch.setenv("CALC_TUTOR_REMARKABLE_HOST", "remarkable")
    monkeypatch.setenv("CALC_TUTOR_REMARKABLE_DIR", str(tmp_path / "mirror"))
    monkeypatch.setattr(rm, "_ssh", dev)
    monkeypatch.setattr(rm, "_download", lambda name, dest: None)  # no PDFs in these tests
    return dev


def _dark(png: bytes, x: int, y: int) -> bool:
    img = Image.open(io.BytesIO(png)).convert("L")
    s = img.width / rm.SCREEN_W  # the page may be downscaled for sending
    return min(img.getpixel((int(x * s) + dx, int(y * s) + dy))
               for dx in range(-3, 4) for dy in range(-3, 4)) < 100


def test_render_places_ink_like_the_device(tmp_path):
    f = tmp_path / "p.rm"
    f.write_bytes(make_rm([[(-500, 300), (-300, 300)], [(100, 1000), (100, 1200)]]))
    ink = rm.read_ink(f)
    assert len(ink.lines) == 2
    img, box = rm.render(ink)
    png = rm._png(img)
    # device x is centred: x=-500 -> 202px from the left edge
    assert _dark(png, 202 + 100, 300) and _dark(png, 702 + 100, 1100)
    assert not _dark(png, 1200, 300) and box is None


def test_new_strokes_by_session_and_last_look():
    h = {"changes": [[1000, ["a"]], [5000, ["a", "b"]], [5300, ["a", "b", "c"]]],
         "looked": None}
    # b and c were written in one session (300 s apart); a predates it
    assert rm.new_strokes(h, {"a", "b", "c"}) == {"b", "c"}
    h["looked"] = [5200, ["a", "b"]]
    assert rm.new_strokes(h, {"a", "b", "c"}) == {"c"}
    assert rm.new_strokes({"changes": [[1, ["a"]]], "looked": None}, {"a"}) is None


def test_match_document():
    docs = {"1": {"visibleName": "scratch pad"},
            "2": {"visibleName": "James Stewart - Calculus, 7th Edition.pdf"},
            "3": {"visibleName": "Principles of Neural Science, Fifth Edition.pdf"}}
    assert rm.match_document("Scratch Pad", docs) == "1"
    assert rm.match_document("stewart calculus", docs) == "2"
    assert rm.match_document("neural science", docs) == "3"
    assert rm.match_document("tax return", docs) is None


def test_look_finds_latest_page_anywhere_and_highlights_new_ink(device):
    nb, (p1, p2) = device.add_doc("scratch pad", [make_rm([[(-400, 200), (0, 200)]]),
                                                  make_rm([[(-400, 400), (0, 400)]])])
    pdf, (pp,) = device.add_doc("Stewart Calculus.pdf", [make_rm([[(0, 900), (200, 900)]])],
                                file_type="pdf", redirs=[41])
    out = rm.look()
    assert out["info"]["document"] == "Stewart Calculus.pdf"
    assert out["info"]["page"] == 42  # PDF page 42 (0-based redir 41)
    assert out["info"]["new_ink"] == "no earlier snapshot to compare with"
    assert {p["document"] for p in out["info"]["recent_pages"]} == {"scratch pad"}
    # PDF background is fetched in the background; we were told so
    assert any("downloading" in n for n in out["info"]["notes"])

    # an hour later the learner writes a question under their notes on the scratch pad
    device.clock += 3600
    device.write_page(nb, p2, make_rm([[(-400, 400), (0, 400)], [(-300, 1500), (300, 1500)]]))
    out = rm.look()
    assert out["info"]["document"] == "scratch pad" and out["info"]["page"] == 2
    assert out["info"]["new_strokes"] == 1 and out["new_png"] is not None
    assert _dark(out["page_png"], 702, 1500)
    assert Path(out["info"]["capture"]).exists()

    # by name and page; and the page before it
    assert rm.look(document="scratch", page=1)["info"]["page"] == 1
    assert rm.look(document="scratch", previous=True)["info"]["page"] == 1
    with pytest.raises(rm.RemarkableError, match="matches"):
        rm.look(document="tax return")


def test_look_offline_uses_mirror(device):
    device.add_doc("SIFT", [make_rm([[(-400, 200), (0, 200)]])])
    rm.look()
    device.online = False
    out = rm.look()
    assert out["info"]["document"] == "SIFT"
    assert any("not reachable" in n for n in out["info"]["notes"])


def test_poll_snapshots_so_next_look_knows_whats_new(device):
    nb, (p1,) = device.add_doc("scratch pad", [make_rm([[(-400, 200), (0, 200)]])])
    assert rm.poll() == 1
    assert rm.poll() == 0  # nothing changed
    device.clock += 3600  # an hour later, a new session
    device.write_page(nb, p1, make_rm([[(-400, 200), (0, 200)], [(-200, 800), (200, 800)]]))
    out = rm.look()
    assert out["info"]["new_strokes"] == 1


async def test_mcp_tool_returns_images(device):
    device.add_doc("scratch pad", [make_rm([[(-400, 200), (0, 200)]])])
    async with Client(mcp) as c:
        res = await c.call_tool("look_at_remarkable", {})
        kinds = [b.type for b in res.content]
        assert kinds[:2] == ["text", "image"]
        assert json.loads(res.content[0].text)["document"] == "scratch pad"
        missing = await c.call_tool("look_at_remarkable", {"document": "nope"})
        assert "error" in json.loads(missing.content[0].text)
