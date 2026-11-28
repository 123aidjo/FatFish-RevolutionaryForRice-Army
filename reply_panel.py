"""Decision controls mounted under the existing authenticated panel."""
from contextlib import closing
import json
import time
from fastapi import APIRouter, HTTPException
from pydantic import Field
from settings import ConfigModel
from reply_config import ReplyConfig, read_config, save_config
from usage_ledger import stats


class Edit(ConfigModel):
    revision: str = Field(pattern=r'^[0-9a-f]{64}$')
    config: ReplyConfig
    clear_key: bool = False


def routes(store,current):
    api=APIRouter(prefix='/reply-decision')

    @api.get('')
    def read():
        cfg,revision=read_config(store)
        values=cfg.model_dump(); values['planner_key']=''
        settings=current().settings
        with closing(store.open_read()) as db:
            scopes=[json.loads(r[0]) for r in db.execute("SELECT DISTINCT scope FROM model_usage WHERE scope<>'' ORDER BY scope")]
            states=[dict(r) for r in db.execute('''SELECT scope,silence,last_decision,last_reply,
                (SELECT count(*) FROM reply_pending p WHERE p.scope=s.scope) pending FROM reply_state s''')]
        return {'revision':revision,'config':values,'key_configured':bool(cfg.planner_key),
                'base_interval':settings.chat.participation_every,'name':settings.persona.name,
                'aliases':settings.persona.aliases,'scopes':scopes,'states':states}

    @api.put('')
    def save(payload:Edit):
        cfg=payload.config
        cfg=cfg.model_copy(update={'planner_key':'' if payload.clear_key else cfg.planner_key or read_config(store)[0].planner_key})
        try: save_config(store,cfg,payload.revision)
        except ValueError: raise HTTPException(409,'配置已变化，请刷新后重试') from None
        return {'saved':True}

    @api.get('/stats')
    def report(scope:str='',since:float=0,until:float|None=None):
        until=time.time() if until is None else until
        if not 0<=since<=until or not all(__import__('math').isfinite(v) for v in (since,until)):
            raise HTTPException(422,'时间范围无效')
        if len(scope)>600: raise HTTPException(422,'会话范围无效')
        if scope:
            try:
                parts=json.loads(scope)
                if not isinstance(parts,list) or len(parts)!=4 or not all(isinstance(p,str) for p in parts): raise ValueError()
                scope=json.dumps(parts,ensure_ascii=False,separators=(',',':'))
            except (TypeError,ValueError): raise HTTPException(422,'会话范围无效') from None
        return stats(store,scope,since,until)

    return api
