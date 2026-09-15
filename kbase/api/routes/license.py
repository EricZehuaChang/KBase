"""许可证续期路由（T16）：管理员上传新的 license.json 完成离线续期。

为什么需要这个端点：enforce=true 时授权到期会拦截**所有**业务端点
（kbase/license.py 的 make_license_guard），客户现场又常常完全离线——
没有"运维手工把文件拷进容器"以外的续期通道，就得靠停机+进机器；而
授权到期的部署里，管理员连设置页都打不开，更需要一个能自助完成闭环的
入口。这条链路的可达性由豁免清单保证（见 kbase/license.py 的
ALLOWED_EXACT / ALLOWED_PREFIXES）：
    到期被拦 → POST /api/auth/login（豁免）→ POST /api/license（豁免，本文件）
    → 新证书落盘 → 业务端点恢复。

安全边界：
- require_admin：能换许可证等于能改授权范围，只看登录态不够；
- 文件内容**先验签再落盘**（install_license_text）——签名由供应商私钥
  签出，管理员上传不了自签的假证书；
- 不开放删除/清空：把 license.json 删掉会退回 trial 态（不拦），等于给了
  一条"删文件绕过拦截"的后门，所以这里只做替换，不做卸载。
"""
from fastapi import File, Request, UploadFile
from fastapi import status as http_status

from kbase import license as license_mod
from kbase.api.routes import RouteDeps
from kbase.api.services import Services
from kbase.errors import AppError

# 上传体积上限（字节）：license.json 是几百字节的小文件，1MB 已经宽松到
# 不可能有正常文件被拒；给上限是为了不让这个豁免端点成为免鉴权大文件入口
# （登录前不可达，但管理员会话被窃取时也应有天花板）。
_MAX_LICENSE_BYTES = 1024 * 1024
_LICENSE_FILENAME = "license.json"


def register(router, svc: Services, deps: RouteDeps) -> None:
    @router.post("/license", status_code=http_status.HTTP_200_OK,
                 dependencies=[deps.require_admin, deps.audit_mutation])
    async def upload_license(request: Request,
                             file: UploadFile | None = File(default=None)):
        """上传新的 license.json（multipart 字段名 file）。

        仅 admin 可调用（require_admin）；上传成功即生效——不需要重启：
        拦截判定按文件 (mtime, size) 失效缓存，原子替换后下一个请求就是
        新证书的判定。响应返回校验通过后的完整许可证状态（含
        edition/seats/features/format），前端据此立刻刷新卡片。
        """
        if file is None:
            raise AppError("error.license_file_required",
                           "缺少上传文件（multipart 字段名 file）", status=422)
        raw_bytes = await file.read()
        if not raw_bytes:
            raise AppError("error.license_file_required", "上传的文件为空",
                           status=422)
        if len(raw_bytes) > _MAX_LICENSE_BYTES:
            raise AppError("error.license_file_too_large",
                           "许可证文件过大（上限 1MB）", status=413)
        try:
            text = raw_bytes.decode("utf-8")
        except UnicodeDecodeError as e:
            raise AppError("error.license_bad_file",
                           "许可证文件必须是 UTF-8 文本：{msg}", status=422,
                           msg=str(e)[:200]) from e

        state = license_mod.install_license_text(text)
        # 返回值 = 落盘后 check_license() 的真实结果（trial/invalid 不可能
        # 出现——刚验签通过），带 grace_days_left 便于前端直接展示剩余宽限。
        return {"ok": True, "filename": file.filename or _LICENSE_FILENAME,
                "license": license_mod.check_license(), "uploaded": state}
