"""真机探针（只读）：坐实 StaticMesh 材质槽 / blend_mode / Nanite 用法标志 / 纹理尺寸
在各版本 UE Python 绑定里的真名，供 prism/domain/optutil.py 候选链取数。

背景：L3 真实工程抽检（UE 5.4 + 客户美术库）发现 scan_nanite_candidates 恒返
materials: [] / blend_modes: [] / blend_unreadable: false —— "半透明材质 + Nanite"
的拦截守卫实际没取到材质，skip_translucent 形同虚设。红了不猜，先探真名。

跑法 A（工程须空闲）：
  UnrealEditor-Cmd.exe <uproject> -run=pythonscript -script="<本文件绝对路径>" -stdout -unattended -nosplash
跑法 B：UE 编辑器 [PY] 控制台 exec(open(r"<本文件>", encoding="utf-8").read())
只读 /Engine 资产，不写 Content；结果同时落 unreal.log 与同目录 probe_result.txt。
"""
import os
import sys

try:
    import unreal
except Exception:
    print("[mat-probe] import unreal FAILED -> 需在 UE 内运行")
    raise SystemExit(1)

LINES = []
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "probe_result.txt")


def emit(text):
    LINES.append(text)
    unreal.log(text)


def flush():
    with io_open() as fh:
        fh.write("\n".join(LINES) + "\n")


def io_open():
    return open(OUT, "w", encoding="utf-8")


def probe(label, fn):
    try:
        emit("%-58s -> %r" % (label, fn()))
    except Exception as exc:
        emit("%-58s -> ERR %s: %s" % (label, type(exc).__name__, exc))


emit("=== python %s ===" % sys.version.split()[0])
probe("unreal.SystemLibrary.get_engine_version",
      lambda: unreal.SystemLibrary.get_engine_version())

mesh = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/Cube")
mat = unreal.EditorAssetLibrary.load_asset("/Engine/BasicShapes/BasicShapeMaterial")

emit("---- StaticMesh material slots ----")
probe("dir(mesh) contains 'material'",
      lambda: [a for a in dir(mesh) if "material" in a.lower()])
probe("get_num_sections(0)", lambda: mesh.get_num_sections(0))
probe("get_lod_used_materials(0)",
      lambda: [m.get_path_name() for m in mesh.get_lod_used_materials(0)])
probe("get_used_materials()",
      lambda: [m.get_path_name() for m in mesh.get_used_materials()])
probe("prop:render_materials",
      lambda: [str(x) for x in mesh.get_editor_property("render_materials")])
probe("prop:static_materials len",
      lambda: len(mesh.get_editor_property("static_materials")))
sms = []
try:
    sms = list(mesh.get_editor_property("static_materials") or [])
except Exception as exc:
    emit("static_materials -> ERR %s" % exc)
if sms:
    e = sms[0]
    emit("---- StaticMaterial element fields (%s) ----" % type(e).__name__)
    probe("dir(element) contains 'material'",
          lambda: [a for a in dir(e) if "material" in a.lower()])
    for name in ("material_interface", "material", "material_type", "socket_name",
                 "imported_material_slot_name", "section_name"):
        probe("getattr(element, %s)" % name,
              lambda nn=name: getattr(e, nn, "<absent>"))
    probe("element.material_interface -> path",
          lambda: e.material_interface.get_path_name())
    probe("element.get_editor_property('material_interface') -> path",
          lambda: e.get_editor_property("material_interface").get_path_name())
    probe("element.get_editor_property('material') -> path",
          lambda: e.get_editor_property("material").get_path_name())

emit("---- Material blend_mode / nanite usage ----")
probe("class(mat)", lambda: type(mat).__name__)
probe("dir(mat) contains 'blend'", lambda: [a for a in dir(mat) if "blend" in a.lower()])
probe("dir(mat) contains 'nanite'", lambda: [a for a in dir(mat) if "nanite" in a.lower()])
probe("prop:blend_mode", lambda: str(mat.get_editor_property("blend_mode")))
probe("prop:blend_mode type", lambda: type(mat.get_editor_property("blend_mode")).__name__)
probe("getattr(mat,'blend_mode')", lambda: str(getattr(mat, "blend_mode")))
probe("prop:two_sided", lambda: mat.get_editor_property("two_sided"))
for name in ("b_used_with_nanite", "used_with_nanite", "nanite_usage",
             "b_available_without_nanite"):
    probe("prop:%s" % name, lambda nn=name: mat.get_editor_property(nn))
probe("unreal.BlendMode members",
      lambda: [x for x in dir(unreal.BlendMode) if x.upper().startswith("BLEND_")])
for asset in ("/Engine/EngineMaterials/WorldGridMaterial",
              "/Engine/EngineMaterials/BasicMaterial",
              "/Engine/Functions/Engine_MaterialFunctions01/Opacity/CameraDepthFade"):
    m2 = unreal.EditorAssetLibrary.load_asset(asset)
    if m2 is None:
        continue
    probe("%s prop:blend_mode" % asset.rpartition("/")[2],
          lambda mm=m2: str(mm.get_editor_property("blend_mode")))
    probe("%s get_blend_mode()" % asset.rpartition("/")[2],
          lambda mm=m2: str(mm.get_blend_mode()))

emit("---- Texture size / max_texture_size ----")
for asset in ("/Engine/EngineResources/WhiteSquareTexture",
              "/Engine/EngineResources/Black",
              "/Engine/EngineResources/DefaultTexture",
              "/Engine/EditorResources/S_Actor"):
    t = unreal.EditorAssetLibrary.load_asset(asset)
    if t is None:
        emit("load %s -> None" % asset)
        continue
    cls = type(t).__name__
    probe("%s prop:max_texture_size" % cls, lambda tt=t: tt.get_editor_property("max_texture_size"))
    probe("%s prop:compression_settings" % cls,
          lambda tt=t: str(tt.get_editor_property("compression_settings")))
    probe("%s blueprint_get_size_x" % cls, lambda tt=t: tt.blueprint_get_size_x())
    probe("%s get_source_size" % cls, lambda tt=t: tt.get_source_size())
    probe("%s source_disk_and_memory_size" % cls,
          lambda tt=t: tt.blueprint_get_texture_source_disk_and_memory_size())
    probe("%s dir(t) contains 'size'" % cls,
          lambda tt=t: [a for a in dir(tt) if "size" in a.lower()])

emit("---- TextureCube ----")
for asset in ("/Engine/EngineResources/GradientTexture_01",
              "/Engine/EditorResources/EmptyActor"):
    c = unreal.EditorAssetLibrary.load_asset(asset)
    if c is None:
        continue
    probe("cube-ish %s class" % asset.rpartition("/")[2], lambda cc=c: type(cc).__name__)

emit("=== done ===")
flush()
