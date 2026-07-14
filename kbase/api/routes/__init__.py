"""路由领域模块：每个文件负责一个业务域，通过 register(router, svc, deps)
把端点挂到共享的 /api APIRouter 上。

约定：
- 端点声明（路径、方法、角色依赖、审计依赖）与原 create_app 内联版本逐一对应，
  行为不变；共享状态一律显式经 Services（kbase/api/services.py）传入。
- deps 里是预先构造好的 fastapi.Depends 对象（见 RouteDeps），保证
  「require_role 在前、审计钩子在后」的依赖顺序全局一致——403 被拒的请求
  不落审计行。"""
from dataclasses import dataclass
from typing import Any


@dataclass
class RouteDeps:
    """各路由域共用的路由级依赖集合。

    require_viewer/editor/admin：spec §3 角色矩阵的最低角色门槛
    （viewer < editor < admin）；audit_mutation：mutating 请求审计钩子，
    必须声明在 require_role 之后（Depends 按声明顺序解析）。"""
    require_viewer: Any
    require_editor: Any
    require_admin: Any
    audit_mutation: Any
