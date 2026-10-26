"""数据层单元测试（t4 交付）：覆盖镜像回归门禁 + 推导字段扩展 + 校验闸门。

运行（**不需要 root，不触碰 /proc，不调 nvidia-smi**）：

    python3 -m unittest discover -s tui/tests -v

回归门禁（tui/DESIGN.md §11 第 4 项）：
对 ``firmware/v100_original_nvflash.rom`` 生成 350 W / 1912 / 1800，
必须与 ``firmware/v100_payload_350w.bin`` 逐字节相同，
sha256 ``8163a67d23ce4de1a2feca5e72ef5b38039d1fa848a7dc08518116f6c26c8096``，
且改动恰为 20 字节。
"""
from __future__ import annotations

import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tui import constants as C                      # noqa: E402
from tui import payload as payload_mod              # noqa: E402
from tui import rom_source, vbios_derive            # noqa: E402

REF_SHA256 = "8163a67d23ce4de1a2feca5e72ef5b38039d1fa848a7dc08518116f6c26c8096"
REF_ROM = REPO / "firmware" / "v100_original_nvflash.rom"
REF_PAYLOAD = REPO / "firmware" / "v100_payload_350w.bin"

CLOCKS_350 = {
    C.FIELD_XBAR: 1912,
    C.FIELD_SYS: 1912,
    C.FIELD_GPC_HDR: 1800,
    C.FIELD_GPC_R1: 1800,
    C.FIELD_PERF0: 1912,
}


def _load_tool(name):
    spec = importlib.util.spec_from_file_location(name, C.TOOLS_DIR / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BaseCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rom = REF_ROM.read_bytes()

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self._env_backup = {k: os.environ.get(k)
                            for k in (C.XDG_CONFIG_HOME, C.XDG_CACHE_HOME, "SUDO_USER")}
        os.environ[C.XDG_CONFIG_HOME] = str(self.tmp / "config")
        os.environ[C.XDG_CACHE_HOME] = str(self.tmp / "cache")
        os.environ.pop("SUDO_USER", None)

    def tearDown(self):
        for key, value in self._env_backup.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()


class TestDeriveExtension(BaseCase):
    """derive_tables 的「仅新增字段」扩展必须存在且语义正确。"""

    def test_performance_exports_entry_size_and_offset(self):
        derive = _load_tool("derive_tables")
        r = derive.derive(bytearray(self.rom))
        perf = r["performance"]
        self.assertIn("entry_size", perf)
        self.assertIn("entry0_3rd_at", perf)
        self.assertEqual(perf["entry0_3rd_at"],
                         perf["last_row"] + perf["entry_size"] + 4)
        # 既有键的语义不得改变：末行第 3 字段仍是 382（= 1528 MHz / 4）
        self.assertEqual(perf["entry0_3rd_u16"], 382)

    def test_existing_keys_still_present(self):
        derive = _load_tool("derive_tables")
        r = derive.derive(bytearray(self.rom))
        for key in ("at", "header_size", "rows", "stride", "last_row", "entry0_3rd_u16"):
            self.assertIn(key, r["performance"])
        row = r["power"]["row"]
        self.assertEqual(row["rated"], 300000)
        self.assertEqual(len(r["power"]["rows"]), 20)

    def test_layout_matches_field_map(self):
        layout = vbios_derive.derive_layout(self.rom)
        self.assertEqual(layout.power_rated_w, 300.0)
        self.assertEqual(layout.xbar.mhz, 1530)
        self.assertEqual(layout.gpc_hdr.mhz, 1312)
        self.assertEqual(layout.performance_entry0_mhz, 1528)
        self.assertTrue(layout.xbar.ratio_consistent)
        self.assertTrue(layout.gpc_hdr.ratio_consistent)
        # 功耗行必须落在唯一的数据镜像内，且该镜像落在 payload 窗口内
        self.assertLessEqual(layout.data_image_start, layout.power_row_at)
        self.assertLess(layout.power_row_at, layout.data_image_end)
        self.assertGreaterEqual(layout.data_image_start, C.PAYLOAD_WINDOW_START)
        self.assertLessEqual(layout.data_image_end,
                             C.PAYLOAD_WINDOW_START + C.PAYLOAD_WINDOW_LENGTH)


class TestPayloadRegression(BaseCase):
    """回归门禁：逐字节等于历史已验证的 payload。"""

    def test_tools_build_payload_matches_reference(self):
        bp = _load_tool("build_payload")
        out = bp.build_payload(self.rom, power_w=350, clocks=dict(CLOCKS_350))
        self.assertEqual(len(out), C.PAYLOAD_WINDOW_LENGTH)
        self.assertEqual(hashlib.sha256(out).hexdigest(), REF_SHA256)
        self.assertEqual(out, REF_PAYLOAD.read_bytes())

    def test_changed_bytes_is_exactly_20(self):
        bp = _load_tool("build_payload")
        out = bp.build_payload(self.rom, power_w=350, clocks=dict(CLOCKS_350))
        base = C.PAYLOAD_WINDOW_START
        diff = [i for i in range(base, base + len(out)) if out[i - base] != self.rom[i]]
        self.assertEqual(len(diff), 20)
        layout = vbios_derive.derive_layout(self.rom)
        # 全部改动都必须落在唯一的数据镜像内
        self.assertGreaterEqual(min(diff), layout.data_image_start)
        self.assertLess(max(diff), layout.data_image_end)

    def test_tui_payload_build_matches_reference(self):
        intent = payload_mod.EditIntent(power_w=350, clocks=dict(CLOCKS_350))
        built = payload_mod.build(self.rom, intent)
        self.assertEqual(built.sha256, REF_SHA256)
        self.assertEqual(built.data, REF_PAYLOAD.read_bytes())
        self.assertEqual(built.summary["changed_bytes"], 20)
        report = payload_mod.verify_roundtrip(built)
        self.assertTrue(report.ok, report.mismatches)
        self.assertTrue(report.size_ok)

    def test_roundtrip_reads_back_all_fields(self):
        intent = payload_mod.EditIntent(power_w=330, clocks={
            C.FIELD_XBAR: 1785, C.FIELD_SYS: 1785,
            C.FIELD_GPC_HDR: 1700, C.FIELD_GPC_R1: 1700,
            C.FIELD_PERF0: 1784,
        })
        built = payload_mod.build(self.rom, intent)
        report = payload_mod.verify_roundtrip(built)
        self.assertTrue(report.ok, report.mismatches)
        self.assertEqual(report.fields[C.FIELD_XBAR], (1785, 1785))


class TestIntentValidation(BaseCase):
    """range / ratio / 同值三类闸门。"""

    def setUp(self):
        super().setUp()
        self.layout = vbios_derive.derive_layout(self.rom)

    def _codes(self, intent):
        return [i.code for i in payload_mod.validate_intent(intent, self.layout)]

    def test_power_out_of_range_rejected(self):
        intent = payload_mod.EditIntent(power_w=400, clocks=dict(CLOCKS_350))
        self.assertIn(C.ERR_RANGE, self._codes(intent))
        with self.assertRaises(payload_mod.PayloadError):
            payload_mod.build(self.rom, intent, layout=self.layout)

    def test_power_below_row_low_rejected(self):
        intent = payload_mod.EditIntent(power_w=140, clocks=dict(CLOCKS_350))
        self.assertIn(C.ERR_RANGE, self._codes(intent))

    def test_clock_out_of_range_rejected(self):
        intent = payload_mod.EditIntent(power_w=350, clocks={C.FIELD_XBAR: 2500})
        self.assertIn(C.ERR_RANGE, self._codes(intent))

    def test_same_as_current_rejected(self):
        current = payload_mod.EditIntent.from_layout(self.layout)
        self.assertIn(C.WARN_SAME_AS_CURRENT, self._codes(current))

    def test_clock_above_physical_limit_is_warning_only(self):
        intent = payload_mod.EditIntent(power_w=350, clocks={C.FIELD_XBAR: 1900})
        codes = self._codes(intent)
        self.assertIn(C.WARN_CLOCK_PHYSICAL, codes)
        self.assertNotIn(C.ERR_RANGE, codes)

    def test_perf_alignment_warning(self):
        intent = payload_mod.EditIntent(power_w=350, clocks={C.FIELD_PERF0: 1911})
        self.assertIn(C.WARN_PERF_ALIGN, self._codes(intent))


class TestPayloadWrite(BaseCase):
    def test_write_produces_file_with_matching_hash(self):
        intent = payload_mod.EditIntent(power_w=350, clocks=dict(CLOCKS_350))
        built = payload_mod.build(self.rom, intent)
        path = payload_mod.write(built, cache_dir=self.tmp / "payloads", name_hint="pow350")
        self.assertTrue(path.is_file())
        self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), REF_SHA256)
        self.assertIn(built.sha8, path.name)


class TestRomSource(BaseCase):
    def test_repo_sample_is_used_and_hashed(self):
        candidate = rom_source.load_rom(prefer_repo=True)
        self.assertEqual(candidate.kind, rom_source.SOURCE_REPO)
        self.assertEqual(candidate.sha256, hashlib.sha256(self.rom).hexdigest())

    def test_explicit_path_used_before_repo_sample(self):
        target = self.tmp / "my.rom"
        target.write_bytes(self.rom)
        candidate = rom_source.load_rom(explicit_path=str(target), prefer_repo=False)
        self.assertEqual(candidate.kind, rom_source.SOURCE_FILE)
        self.assertEqual(candidate.size, len(self.rom))

    def test_too_short_rom_rejected(self):
        short = self.tmp / "short.rom"
        short.write_bytes(b"\x00" * 1024)
        with self.assertRaises(rom_source.RomError):
            rom_source.load_rom(explicit_path=str(short), prefer_repo=False)


class TestDeriveFailureModes(BaseCase):
    def test_garbage_rom_raises_derive_error(self):
        with self.assertRaises(vbios_derive.DeriveError):
            vbios_derive.derive_layout(b"\x00" * (C.PAYLOAD_WINDOW_START
                                                 + C.PAYLOAD_WINDOW_LENGTH))

    def test_short_rom_raises_with_code(self):
        with self.assertRaises(vbios_derive.DeriveError) as ctx:
            vbios_derive.derive_layout(b"\x00" * 512)
        self.assertEqual(ctx.exception.code, C.ERR_ROM_TOO_SHORT)


if __name__ == "__main__":
    unittest.main(verbosity=2)
