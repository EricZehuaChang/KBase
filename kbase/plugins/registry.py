class PluginRegistry:
    def __init__(self):
        self._plugins: dict[tuple[str, str], type] = {}

    def register(self, kind: str, name: str):
        def deco(cls):
            self._plugins[(kind, name)] = cls
            return cls
        return deco

    def create(self, kind: str, name: str, **kwargs):
        key = (kind, name)
        if key not in self._plugins:
            known = [n for k, n in self._plugins if k == kind]
            raise KeyError(f"插件未注册: {kind}/{name}，已注册: {known}")
        return self._plugins[key](**kwargs)


registry = PluginRegistry()   # 全局单例，实现文件在模块加载时向它注册
