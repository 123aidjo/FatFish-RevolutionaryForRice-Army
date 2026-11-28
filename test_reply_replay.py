"""Offline, fictional fixtures only. No network and no real QQ adapter or credentials."""
import json
from contextlib import closing
from tempfile import TemporaryDirectory
from pathlib import Path
import time

from dispatcher import Dispatcher
from llm import Completion, Usage
from reply_config import read_config, save_config
from test_qq_engine import Harness
from usage_ledger import stats


class FixedDispatcher(Dispatcher):
    def _record_event(self,event,now,**kwargs):
        # Original V2 Store.accept count policy, without dynamic scheduling or target candidates.
        from dataclasses import replace
        event=replace(event,memory_allowed=True)
        return self.store.accept(event,self.settings.chat.participation_every,now,mode=self.mode,
                                 reply_block=self._reply_block(event))


class ReplayModel:
    def __init__(self): self.calls=0
    async def complete(self,system,user,**kwargs):
        self.calls+=1; body=json.loads(user)
        if kwargs['max_tokens']==128: value={'approved':True}
        elif 'messages' in body:  # Planner: only the actually budgeted candidates are visible.
            choices=[m for m in body['messages'] if '小鱼' in m['text']]
            value={'shouldReply':bool(choices)}
            if choices: value['targetMessageId']=choices[-1]['id']
        else:
            choices=[m for m in body.get('候选回复消息',[]) if '小鱼' in m['text']]
            interested=bool(choices) or '小鱼' in body['当前消息']['正文']
            value={'shouldSend':interested}
            if interested:
                value.update(text='可以，一起分析这个问题。',emotion='joy',intensity=1,sticker=None)
                if choices: value['targetMessageId']=choices[-1]['id']
        content=json.dumps(value,ensure_ascii=False)
        return Completion(content,Usage(len((system+user).encode()),len(content.encode()),estimated=True))


def replay(path,variant):
    h=Harness(path); model=ReplayModel();h.model=model
    h.settings=h.settings.model_copy(update={'memory':h.settings.memory.model_copy(update={
        'summary_messages':10000,'summary_characters':1000000,'summary_seconds':1000000})})
    h.restart()
    if variant=='fixed':
        h.engine=FixedDispatcher(h.store,h.settings,h.engine.adapter,model,clock=lambda:h.now)
    else:
        cfg,revision=read_config(h.store)
        save_config(h.store,cfg.model_copy(update={'mode':variant,'early_name':True,'max_wait':3600}),revision)
        h.engine.planner_model=model
    expected={5,40,85};latencies=[]
    for number in range(1,97):
        h.now+=1
        text='小鱼，帮我分析这场游戏的打法？' if number in expected else '我们继续聊天气变化，今天的云层很多。'
        started=time.perf_counter()
        h.ingest(number=number,mention=None,text=text,moment=h.now)
        latencies.append(time.perf_counter()-started)
    sent={int(call['message'][0]['data']['id']) for call in h.rpc.sends}
    report=stats(h.store,'',0,time.time()+30)
    mode='balanced' if variant=='balanced' else 'eco'
    total=report['totals'][mode]
    # Fixed rounds predate reply_rounds. Attribute silent calls directly from the content-free usage and Outbox.
    with closing(h.store.open_read()) as db:
        silent_tokens=db.execute("""SELECT coalesce(sum(u.prompt+u.completion),0) FROM model_usage u JOIN outbox o ON o.id=u.job_id
            WHERE o.detail IN ('model_declined','planner_declined')""").fetchone()[0]
    return {'variant':variant,'messages':96,'tokens_estimated':total['tokens'],'model_requests':model.calls,
        'silent_tokens_estimated':silent_tokens,'simulated_replies':len(sent),'missed':sorted(expected-sent),
        'unwanted':sorted(sent-expected),'valid_target_rate':len(sent&expected)/len(sent) if sent else None,
        'mean_local_latency_ms':round(sum(latencies)/len(latencies)*1000,3),
        'latency_note':'Local mock and SQLite time only; not real model latency or naturalness.'}


def experiment(root):
    return [replay(root/(variant+'.db'),variant) for variant in ('fixed','eco','balanced')]


def test_offline_comparison_saves_calls_without_missing_labelled_directed_turns(tmp_path):
    fixed,eco,balanced=experiment(tmp_path)
    assert eco['model_requests']<fixed['model_requests']
    assert balanced['tokens_estimated']<fixed['tokens_estimated']
    assert not eco['missed'] and not balanced['missed']
    assert not eco['unwanted'] and not balanced['unwanted']
    assert eco['valid_target_rate']==balanced['valid_target_rate']==1


if __name__=='__main__':
    with TemporaryDirectory(prefix='fatfish-reply-replay-') as temp:
        print(json.dumps(experiment(Path(temp)),ensure_ascii=False,indent=2))
