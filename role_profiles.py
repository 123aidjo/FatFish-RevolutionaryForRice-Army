"""Local role registry. A worker is pinned to one role for its entire lifetime."""
import json
import os
from pathlib import Path
import re
from uuid import uuid4

import tomli_w
import tomllib

PROJECT = Path(__file__).resolve().parent
PRESETS = {
    '默认风格': None,
    '理性助手': '你是一个严谨、耐心的 AI 助手。清楚表达依据和不确定性，帮助用户解决问题，不编造个人经历。',
    '温柔伙伴': '你是温柔、善于倾听的 AI 伙伴。尊重用户的选择和现实关系，诚实表达自己的 AI 身份，不编造现实经历。',
}


def root_directory():
    if os.getenv('FISHBOT_DATA_DIR'):
        return Path(os.environ['FISHBOT_DATA_DIR']).expanduser().resolve()
    if os.name == 'nt':
        return Path(os.getenv('LOCALAPPDATA') or Path.home()/'AppData/Local')/'FishbotV2Public'
    return Path.home()/'.local/share/fishbot-v2-public'


def write_json(path, value):
    from panel_config import atomic_write
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode('utf-8'))


class Profiles:
    def __init__(self, root=None, project=PROJECT):
        self.root = root or root_directory()
        self.project = project
        self.path = self.root/'roles.local.json'

    def read(self):
        if not self.path.exists():
            return {'active':'default', 'roles':{'default':'默认角色'}}
        data = json.loads(self.path.read_text(encoding='utf-8'))
        if not isinstance(data,dict) or not isinstance(data.get('roles'),dict) or data.get('active') not in data['roles']:
            raise ValueError('角色索引损坏，未覆盖任何数据。')
        for key,name in data['roles'].items():
            if not re.fullmatch(r'default|[0-9a-f]{32}',key) or not isinstance(name,str) or not 1<=len(name)<=50:
                raise ValueError('角色索引格式无效。')
        return data

    def data(self, identifier):
        if identifier not in self.read()['roles']:
            raise ValueError('角色不存在。')
        return self.root if identifier=='default' else self.root/'roles'/identifier/'data'

    def config(self, identifier):
        self.data(identifier)
        return self.root/'roles'/identifier/'config'

    def initialize(self):
        data = self.read()
        folder = self.config('default')
        folder.mkdir(parents=True,exist_ok=True)
        for name in ('persona.toml','platforms.toml'):
            target = folder/name
            if not target.exists():
                with target.open('xb') as stream:
                    stream.write((self.project/name).read_bytes())
        if not self.path.exists():
            write_json(self.path,data)

    def create(self, name, preset='默认风格', prompt=None):
        name = name.strip()
        if not 1<=len(name)<=50 or preset not in PRESETS:
            raise ValueError('角色名称须为 1–50 字，并选择有效预设。')
        if prompt is not None and not 1<=len(prompt.strip())<=10000:
            raise ValueError('自定义人设须为 1–10000 字。')
        self.initialize()
        data = self.read()
        if name in data['roles'].values():
            raise ValueError('角色名称已存在。')
        identifier = uuid4().hex
        folder = self.root/'roles'/identifier/'config'
        folder.mkdir(parents=True)
        persona = tomllib.loads((self.project/'persona.toml').read_text(encoding='utf-8'))
        persona.update(name=name,aliases=[name])
        if prompt or PRESETS[preset]:
            persona['system_prompt'] = prompt or PRESETS[preset]
        platforms = tomllib.loads((self.project/'platforms.toml').read_text(encoding='utf-8'))
        for cfg in platforms['platforms'].values():
            cfg.update(enabled=False,receive=False,send=False,allowlist=[])
        for key in list(platforms):
            if key.startswith('qq_owner'):
                del platforms[key]
        for filename,value in (('persona.toml',persona),('platforms.toml',platforms)):
            (folder/filename).write_text(tomli_w.dumps(value),encoding='utf-8')
        data['roles'][identifier] = name
        write_json(self.path,data)
        return identifier

    def rename(self, identifier, name):
        data = self.read()
        if identifier not in data['roles'] or not 1<=len(name.strip())<=50:
            raise ValueError('角色或名称无效。')
        if any(k!=identifier and v==name.strip() for k,v in data['roles'].items()):
            raise ValueError('角色名称已存在。')
        data['roles'][identifier] = name.strip()
        write_json(self.path,data)

    def activate(self, identifier):
        self.data(identifier)
        for name in ('persona.toml','platforms.toml'):
            if not (self.config(identifier)/name).is_file():
                raise ValueError('角色配置不完整，不能切换。')
        data = self.read()
        data['active'] = identifier
        write_json(self.path,data)


def active_id():
    return os.getenv('FISHBOT_PROFILE_ID') or Profiles().read()['active']


def config_directory(project=PROJECT):
    manager = Profiles()
    if not manager.path.exists() and active_id() == 'default':
        return project
    return manager.config(active_id())


def isolated():
    return active_id() != 'default'


def child_environment(identifier=None):
    from local_runtime import ENVIRONMENT
    env = dict(os.environ)
    identifier = identifier or active_id()
    env['FISHBOT_PROFILE_ID'] = identifier
    if identifier != 'default':
        # No inherited default-role login, owner policy, model or visual credentials.
        for key in list(env):
            if key in ENVIRONMENT.values() or key.startswith(('FISHBOT_QQ_', 'FISHBOT_ONEBOT_', 'FISHBOT_NAPCAT_', 'FISHBOT_VISION_')):
                env.pop(key,None)
    return env


from contextlib import contextmanager


@contextmanager
def worker_lock():
    """OS-released per-role lock, also covering independent command-line launches."""
    from local_runtime import data_directory
    path = data_directory()/'role-worker.lock'
    path.parent.mkdir(parents=True,exist_ok=True)
    with path.open('a+b') as stream:
        stream.seek(0,2)
        if stream.tell()==0:
            stream.write(b'0'); stream.flush()
        stream.seek(0)
        try:
            if os.name=='nt':
                import msvcrt
                msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
        except OSError:
            raise ValueError('该角色已有服务运行，不能重复启动。') from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name=='nt':
                msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
            else:
                fcntl.flock(stream.fileno(),fcntl.LOCK_UN)


def switch_service(service, identifier, manager=None, save_current=None):
    manager = manager or Profiles()
    old = active_id()
    if identifier == old:
        return '已经是当前角色。'
    # Validate before stopping anything. Never change a running worker's directory.
    from settings import load_settings
    load_settings(manager.config(identifier),database_path=manager.data(identifier)/'fishbot.db')
    running = service.running()
    service.stop()
    try:
        if save_current:
            save_current()
        manager.activate(identifier)
        os.environ['FISHBOT_PROFILE_ID'] = identifier
        service.log = manager.data(identifier)/'launcher/native-server.log'
        service.port = load_settings(manager.config(identifier),database_path=manager.data(identifier)/'fishbot.db').server.port
        if running:
            service.start()
    except Exception:
        service.stop()
        manager.activate(old)
        os.environ['FISHBOT_PROFILE_ID'] = old
        service.log = manager.data(old)/'launcher/native-server.log'
        service.port = load_settings(manager.config(old),database_path=manager.data(old)/'fishbot.db').server.port
        if running:
            service.start()
        raise
    return '角色已切换；面板需使用该角色的访问令牌重新登录。'
