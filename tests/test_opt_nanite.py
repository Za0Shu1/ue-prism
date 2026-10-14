"""v1.5 Nanite 工具族离线用例：跨版本属性漂移、半透明拦截、双钥守卫、降级结构。

Nanite 开关在 5.0/5.1 是 bSupportsNanite、5.3 是 NaniteSettings.enabled、5.4 又变，
所以属性读写必须多候选 + 回读校验。这里用最小替身对象把三条路径都钉住，
真机命中哪条签名由 L2 夹具的 tried 记录（docs/TESTING.md 兼容矩阵）。
"""
from __future__ import annotations

from prism import envelope
from prism.domain import opt_nanite as on
from prism.domain import optutil as ou


class _Props(object):
    """最小 unreal 对象替身：只有 get/set_editor_property，且禁止裸 setattr（同 Python 绑定）。"""

    def __init__(self, props=None, read_only=(), missing=(), writable=()):
        object.__setattr__(self, "_p", dict(props or {}))
        object.__setattr__(self, "_ro", set(read_only))
        object.__setattr__(self, "_missing", set(missing))
        object.__setattr__(self, "_w", set(writable))

    def get_editor_property(self, key):
        if key in self._missing or key not in self._p:
            raise AttributeError(key)
        return self._p[key]

    def set_editor_property(self, key, value):
        # 真实绑定只对"该版本存在的可编辑属性"成功，其余抛异常（这才逼出多候选兜底）
        if key in self._ro or key in self._missing:
            raise AttributeError("read only / unavailable: " + key)
        if key not in self._p and key not in self._w:
            raise AttributeError("no such editable property: " + key)
        self._p[key] = value

    def __setattr__(self, key, value):
        if key.startswith("_"):
            object.__setattr__(self, key, value)
            return
        raise AttributeError("绑定不允许直接赋值: " + key)


class _Mesh(_Props):
    def __init__(self, props=None, mats=(), **kw):
        _Props.__init__(self, props, **kw)
        object.__setattr__(self, "_mats", list(mats))

    def get_lod_used_materials(self, lod):
        return list(self._mats)


class _Mat(_Props):
    def __init__(self, props=None, path="/Game/Mats/M_Test"):
        _Props.__init__(self, props)
        object.__setattr__(self, "_path", path)

    def get_path_name(self):
        return self._path + "." + self._path.rpartition("/")[2]


class _MatInterface(_Mat):
    """MaterialInterface 替身：接口上没有 used_with_nanite，解包基类材质才读得到。

    5.4 真机暴露的形状：static_materials[i].material_interface 给的就是这一类对象。
    """

    def __init__(self, props=None, base=None, path="/Game/Mats/M_Inst"):
        _Mat.__init__(self, props, path=path)
        object.__setattr__(self, "_base", base)

    def get_base_material(self):
        return self._base


# ---------- 读：三条版本签名 ----------

def test_read_nanite_flat_54():
    mesh = _Mesh({"nanite_enabled": True})
    value, api, _tried = on._read_nanite(mesh)
    assert value is True and api == "nanite_enabled"


def test_read_nanite_supports_nanite_50_51():
    mesh = _Mesh({"supports_nanite": False})
    value, api, _tried = on._read_nanite(mesh)
    assert value is False and api == "supports_nanite"


def test_read_nanite_struct_53():
    mesh = _Mesh({"nanite_settings": _Props({"enabled": True})})
    value, api, _tried = on._read_nanite(mesh)
    assert value is True and api == "nanite_settings.enabled"


def test_read_nanite_all_missing_is_none_not_crash():
    mesh = _Mesh({})
    value, api, tried = on._read_nanite(mesh)
    assert value is None and api is None and tried


# ---------- 写：回读校验才算成功 ----------

def test_write_nanite_flat_and_verify():
    mesh = _Mesh({"nanite_enabled": False})
    ok, api, _tried = on._write_nanite(None, mesh, True)
    assert ok is True and mesh.get_editor_property("nanite_enabled") is True
    assert "nanite_enabled" in api


def test_write_nanite_falls_back_to_struct():
    settings = _Props({"enabled": False})
    mesh = _Mesh({"nanite_settings": settings}, read_only=("nanite_enabled",))
    ok, api, _tried = on._write_nanite(None, mesh, True)
    assert ok is True
    assert "nanite_settings.enabled" in api
    assert settings.get_editor_property("enabled") is True


def test_write_nanite_honest_failure_when_no_api():
    mesh = _Mesh({}, missing=("nanite_enabled", "supports_nanite"))
    ok, api, tried = on._write_nanite(None, mesh, True)
    assert ok is False and api is None and tried


# ---------- 材质半透明判定 ----------

def test_material_view_flags_translucent_blend():
    mats = [_Mat({"blend_mode": "BLEND_Translucent", "used_with_nanite": True}),
            _Mat({"blend_mode": "BLEND_Opaque"})]
    view = on._material_view(None, _Mesh({}, mats=mats))
    assert view["translucent"] is True and view["blend_unreadable"] is False
    assert view["api"] == "get_lod_used_materials(0)"
    assert {r["material"] for r in view["materials"]} == {"/Game/Mats/M_Test"}
    assert len(view["materials"]) == 2
    # 未标 used_with_nanite 的材质要汇报（开 Nanite 后不生效的常见原因）
    assert view["materials_need_used_with_nanite"] == 1


def test_material_view_modulate_and_additive_are_translucent():
    for mode in ("BLEND_Modulate", "BLEND_Additive", "BLEND_AlphaComposite"):
        view = on._material_view(None, _Mesh({}, mats=[_Mat({"blend_mode": mode})]))
        assert view["translucent"] is True, mode


def test_material_view_unreadable_blend_is_not_assumed_safe():
    view = on._material_view(None, _Mesh({}, mats=[_Mat({})]))
    assert view["blend_unreadable"] is True and view["translucent"] is False


def test_base_material_three_states():
    """解包三态：方法命中 / 属性命中 / 都不中（原样返回，调用方按读不到降级）。"""
    base = _Mat({"used_with_nanite": True})
    obj, api, tried = ou.base_material(None, _MatInterface({}, base=base))
    assert obj is base and api == "get_base_material()" and tried
    obj2, api2, _t2 = ou.base_material(None, _Mat({"base_material": base}))
    assert obj2 is base and api2 == "prop:base_material"
    plain = _Mat({"blend_mode": "BLEND_Opaque"})
    obj3, api3, tried3 = ou.base_material(None, plain)
    assert obj3 is plain and api3 is None and tried3
    empty, api4, _t4 = ou.base_material(None, _MatInterface({}, base=None))
    assert empty is not None and api4 is None


def test_material_view_reads_usage_from_base_material():
    """真机回归：used_with_nanite 挂在 UMaterial 上，接口直读恒 miss，
    旧实现整份材质清单一律 used_with_nanite=null（某真实工程 5/5 材质如此）。"""
    base = _Mat({"used_with_nanite": True})
    iface = _MatInterface({"blend_mode": "BLEND_Opaque"}, base=base,
                          path="/Game/Mats/M_Inst")
    view = on._material_view(None, _Mesh({}, mats=[iface]))
    row = view["materials"][0]
    assert row["blend_mode"] == "opaque"
    assert row["used_with_nanite"] is True
    assert row["usage_api"] == "get_base_material()>used_with_nanite"
    assert row["material"] == "/Game/Mats/M_Inst"
    assert view["materials_need_used_with_nanite"] == 0


def test_material_view_usage_stays_null_when_unreadable():
    """解包也读不到就留 null（不猜 False）：仍需补标 bUsedWithNanite 的计数照旧。"""
    iface = _MatInterface({"blend_mode": "BLEND_Opaque"}, base=_Mat({}))
    view = on._material_view(None, _Mesh({}, mats=[iface]))
    row = view["materials"][0]
    assert row["used_with_nanite"] is None and row["usage_api"] is None
    assert view["materials_need_used_with_nanite"] == 1


def test_triangles_unmeasurable_returns_none():
    tris, slots = on._triangles(None, _Mesh({}), [])
    assert tris is None and slots is None


# ---------- 工具级契约 ----------

def test_scan_degrades_with_structure_keys():
    r = on.scan_nanite_candidates(folder="/Game/Meshes", top=3)
    assert r["note"] == "no_engine"
    assert {"items", "summary", "candidates", "thresholds", "total", "truncated",
            "cap", "tried", "rejected", "scope", "note"} <= set(r)
    assert set(r["candidates"]) == {"enable", "conflicts", "blocked_by_translucent",
                                    "materials_need_used_with_nanite"}
    assert r["summary"]["enable_candidate"] == 0


def test_scan_threshold_and_cap_are_reported():
    r = on.scan_nanite_candidates(min_triangles=999999, top=7)
    assert r["thresholds"]["min_triangles"] == 999999 and r["cap"] == 7


def test_write_ops_require_double_key():
    for fn in (on.opt_mesh_set_nanite, on.fix_nanite_translucent):
        env = fn(paths="/Game/M/X", dry_run=False, confirm=False)
        assert env["ok"] is False and "双钥" in env["error"]["message"]
        assert env["error"]["code"] == envelope.Code.RUNTIME_ERROR


def test_write_ops_default_dry_run_and_no_engine_honesty():
    env = on.opt_mesh_set_nanite(folder="/Game/Meshes")
    assert env["ok"] is True and env["result"]["dry_run"] is True
    assert env["result"]["executed"] is False and env["result"]["note"] == "no_engine"
    assert env["result"]["enabled"] is True and env["result"]["skip_translucent"] is True
    env2 = on.fix_nanite_translucent(folder="/Game/Meshes", dry_run=False, confirm=True)
    assert env2["ok"] is True and env2["result"]["executed"] is False
    assert env2["result"]["note"] == "no_engine"      # 没引擎就明说没执行



# ---------- 材质槽读取（5.4 真机探针坐实的字段名）----------

from prism.domain import optutil as ou


class _Slot54(_Props):
    """5.4 实证：FStaticMaterial 元素只有 material_interface / material_slot_name，
    没有 material 这个字段名。"""

    def __init__(self, mat=None, name="MaterialSlot0"):
        props = {"material_slot_name": name}
        if mat is not None:
            props["material_interface"] = mat
        _Props.__init__(self, props)


class _Mesh54(_Mesh):
    """5.4 形状的 StaticMesh：static_materials 属性 + get_material(index)/get_num_sections。
    5.4 里 get_lod_used_materials / get_used_materials / render_materials 都不存在。"""

    def __init__(self, slots=(), with_sections=True):
        _Mesh.__init__(self, {"static_materials": list(slots)})
        object.__setattr__(self, "_slots", list(slots))
        object.__setattr__(self, "_no_sections", not with_sections)

    def get_num_sections(self, lod):
        if self._no_sections:
            raise AttributeError("no get_num_sections on this build")
        return len(self._slots)

    def get_material(self, index):
        return self._slots[index].get_editor_property("material_interface")


def test_mesh_materials_54_reads_material_interface():
    m1 = _Mat({"blend_mode": "BLEND_Opaque"}, path="/Game/Mats/M_A")
    m2 = _Mat({"blend_mode": "BLEND_Translucent"}, path="/Game/Mats/M_B")
    mats, api, _tried, unreadable = ou.mesh_materials(None, _Mesh54([_Slot54(m1), _Slot54(m2)]))
    assert api == "prop:static_materials" and unreadable is False
    assert [m.get_path_name().split(".")[0] for m in mats] == ["/Game/Mats/M_A", "/Game/Mats/M_B"]


def test_mesh_materials_unreadable_slots_are_flagged_not_silently_empty():
    """真机回归：旧实现这里返回 ([], None, tried)，scan 给出 blend_unreadable=False，
    "Nanite + 半透明"的拦截形同虚设。槽位在但取不到材质必须标 unreadable。"""
    mesh = _Mesh54([_Slot54(), _Slot54()])
    mats, api, tried, unreadable = ou.mesh_materials(None, mesh)
    assert mats == [] and api is None and unreadable is True
    assert any("static_materials" in str(t.get("sig", "")) for t in tried)


def test_mesh_materials_falls_back_to_get_material_by_section():
    mat = _Mat({"blend_mode": "BLEND_Opaque"})
    mesh = _Mesh54([_Slot54(mat)])
    object.__setattr__(mesh, "_p", {})          # 该版本没有 static_materials
    mats, api, _tried, unreadable = ou.mesh_materials(None, mesh)
    assert api == "get_material(i)" and len(mats) == 1 and unreadable is False


def test_material_view_blocks_when_slots_unreadable():
    view = on._material_view(None, _Mesh54([_Slot54(), _Slot54()]))
    assert view["blend_unreadable"] is True and view["translucent"] is False
    assert view["materials"] == []


def test_material_view_reads_translucent_from_real_54_enum_text():
    mesh = _Mesh54([_Slot54(_Mat({"blend_mode": "<BlendMode.BLEND_TRANSLUCENT: 1>"}))])
    view = on._material_view(None, mesh)
    assert view["translucent"] is True and view["blend_unreadable"] is False
    assert view["materials"][0]["blend_mode"] == "translucent"


def test_blend_mode_of_prefers_interface_method():
    """get_blend_mode() 挂在 MaterialInterface 基类上 -> 材质实例也能直读（5.4 实证）。"""
    class _MatIf(_Mat):
        def get_blend_mode(self):
            return unreal_BlendTranslucent()

    def unreal_BlendTranslucent():
        return "<BlendMode.BLEND_TRANSLUCENT: 1>"

    mode, trans, hit, readable = ou.blend_mode_of(None, _MatIf({"two_sided": True}))
    assert trans is True and readable is True and hit == "get_blend_mode()"


def test_blend_mode_of_never_borrows_two_sided_as_blend_mode():
    """真机回归：取不到 blend_mode 时旧实现退到 two_sided(bool)，
    把"读不到"伪装成"已读且安全"（False -> 不半透明）。"""
    mode, trans, hit, readable = ou.blend_mode_of(None, _Mat({"two_sided": False}))
    assert mode is None and trans is False and hit is None and readable is False


def test_blend_mode_of_unknown_enum_is_unreadable():
    mode, trans, _hit, readable = ou.blend_mode_of(None, _Mat({"blend_mode": "BLEND_NEXT_5_X"}))
    assert trans is False and readable is False


def test_blend_mode_of_matches_underscore_enum_names():
    """真机回归：旧的字串包含判断让 BLEND_ALPHA_COMPOSITE 匹配不上 "alphacomposite"。"""
    cases = {"BLEND_ALPHA_COMPOSITE": True, "BLEND_MODULATE": True, "BLEND_ADDITIVE": True,
             "BLEND_ALPHA_HOLDOUT": True, "BLEND_TRANSLUCENT_COLORED_TRANSMITTANCE": True,
             "BLEND_OPAQUE": False, "BLEND_MASKED": False}
    for name, want in sorted(cases.items()):
        _mode, trans, _hit, readable = ou.blend_mode_of(None, _Mat({"blend_mode": name}))
        assert readable is True and trans is want, name
