from contextlib import closing
import re
import shutil
from time import time
import tomllib

from fastapi.testclient import TestClient
import pytest
import tomli_w

from cognition import Cognition
from cognitive_store import CognitiveStore
from events import Event
from panel import create_app, panel_token
from settings import CognitionSettings, PROJECT_ROOT
from store import Store


HOST = "http://127.0.0.1:8000"
M = "/api/cognition"
GROUP = {"platform":"qq","kind":"group","channel":"300","mode":"simulation"}
DEMO = {**GROUP,"channel":"demo-room","dataset":"demo"}
SYSTEM = {"platform":"system","kind":"role","channel":"fishbot-v2","mode":"live"}
MARKUP = '<img src=x onerror="window.pwned=1"><script>alert(1)</script>'


def seeded(store, platform="qq", channel="300", subject="author-1"):
    engine=Cognition(store,CognitionSettings(extraction=True,reflection=True))
    now=time()
    for i,text in enumerate(("我叫小明，我喜欢羽毛球。","今天练习了 Python 循环。",
                             "今天讨论 Python 列表。","我们来聊头像 "+MARKUP),1):
        e=Event(platform,channel,"group",str(i),subject,"小明",text,False,False,None,[],
                now - (8-i)*3600,memory_allowed=True)
        store.accept(e,0,e.timestamp,mode="simulation",reply_block="test",cognition=True)
        assert engine.run_one("simulation",lambda _: True)


@pytest.fixture()
def workspace(tmp_path):
    store=Store(tmp_path/"official.db")
    store.initialize()
    seeded(store)
    seeded(store,channel="301",subject="author-1")
    seeded(store,channel="302",subject="author-1")
    seeded(store,platform="bilibili")
    CognitiveStore(store).observe_launch("鲸小鱼","observed")
    files=tmp_path/"cfg"
    files.mkdir()
    shutil.copyfile(PROJECT_ROOT/"persona.toml",files/"persona.toml")
    cfg=tomllib.loads((PROJECT_ROOT/"platforms.toml").read_text(encoding="utf-8"))
    cfg["platforms"]["qq"].update(enabled=True,receive=True,allowlist=["300","301"],record_memory=True)
    (files/"platforms.toml").write_text(tomli_w.dumps(cfg),encoding="utf-8")
    app=create_app(store,config_dir=files,data_dir=tmp_path)
    with TestClient(app,base_url=HOST,client=("127.0.0.1",52000)) as client:
        yield client,store,{"Authorization":"Bearer "+panel_token(store,tmp_path),"Origin":HOST}


def get(client, headers, path, params=None):
    res=client.get(M+path,params=params,headers=headers)
    assert res.status_code==200,res.text
    return res.json()


def test_m3_stats_demo_isolation_readiness_and_assets(workspace):
    client,store,headers=workspace
    actual=get(client,headers,"")
    assert not any(actual["settings"][k] for k in ("extraction","recall","reflection"))
    assert all(scope["scope"][2]!="302" for scope in actual["scopes"])
    assert actual["stats"]["jobs"]["done"]==8
    assert actual["stats"]["kinds"]["autobiography"]==1
    preview=get(client,headers,"",{"dataset":"demo"})
    assert preview["dataset"]=="demo" and preview["stats"]["kinds"]["reflection"]==1
    assert preview["stats"]["kinds"]["fact"]>=1
    assert get(client,headers,"")==actual
    with closing(store.open_read()) as db:
        assert db.execute("SELECT count(*) FROM events WHERE channel_id='demo-room'").fetchone()[0]==0
    js=client.get("/assets/memory-workspace.js")
    assert js.status_code==200 and "innerHTML" not in js.text
    assert "textContent" in js.text
    assert "object-src 'none'" in js.headers["Content-Security-Policy"]


def test_m3_graph_scoped_edges_pagination_isolation_and_no_fake_connections(workspace):
    client,_,headers=workspace
    solo=get(client,headers,"/graph",SYSTEM)
    assert len(solo["nodes"])==1 and not solo["edges"]
    empty=get(client,headers,"/graph",{**GROUP,"node_kind":"autobiography"})
    assert empty["nodes"]==[] and empty["edges"]==[]
    first=get(client,headers,"/graph",{**DEMO,"limit":3})
    second=get(client,headers,"/graph",{**DEMO,"limit":3,"offset":3})
    assert first["total"]>6 and {n["id"] for n in first["nodes"]}.isdisjoint(n["id"] for n in second["nodes"])
    graph=get(client,headers,"/graph",{**DEMO,"limit":30})
    nodes={n["id"] for n in graph["nodes"]}
    assert graph["edges"] and all(e["origin_id"] in nodes and e["target_id"] in nodes for e in graph["edges"])
    anchor=graph["edges"][0]["origin_id"]
    sub=get(client,headers,"/graph",{**DEMO,"anchor":anchor})
    adjacent={e["origin_id"] if e["target_id"]==anchor else e["target_id"] for e in graph["edges"]
              if anchor in (e["origin_id"],e["target_id"])}
    assert {n["id"] for n in sub["nodes"]}=={anchor,*adjacent}
    assert client.get(M+"/graph",params={**GROUP,"channel":"301","anchor":1},headers=headers).status_code==404


def test_m3_graph_bounds_large_conversation(workspace):
    client,store,headers=workspace
    scope='["qq","group","300","simulation"]'
    with store.transaction() as db:
        db.executemany("""INSERT INTO memory_nodes(scope,subject,slot,kind,topic,value,state,origin,
            importance,confidence,created_at,updated_at) VALUES (?,'large',?,'fact','负载测试',?,'active',
            'message',2,.8,?,?)""",((scope,f"load-{i}",str(i),time(),time()) for i in range(600)))
    first=get(client,headers,"/graph",{**GROUP,"limit":40})
    second=get(client,headers,"/graph",{**GROUP,"limit":40,"offset":40})
    assert first["total"]>=600 and len(first["nodes"])==len(second["nodes"])==40
    assert {row["id"] for row in first["nodes"]}.isdisjoint(row["id"] for row in second["nodes"])
    assert client.get(M+"/graph",params={**GROUP,"limit":41},headers=headers).status_code==422


def test_m3_profiles_timeline_search_and_confidence(workspace):
    client,_,headers=workspace
    people=get(client,headers,"/people",DEMO)["items"]
    assert len([p for p in people if "小明" in p["alias"]])==2
    assert len({p["subject"] for p in people})==2
    one=get(client,headers,"/people",GROUP)["items"][0]
    other=get(client,headers,"/people",{**GROUP,"channel":"301"})["items"][0]
    assert one["subject"]==other["subject"]
    aliases=get(client,headers,"/nodes",{**GROUP,"q":"小明","limit":1})
    assert aliases["items"] and aliases["more"]
    terms=get(client,headers,"/nodes",{**GROUP,"q":"羽毛球","node_kind":"preference","confidence":.85,
                                      "sort":"importance"})["items"]
    assert len(terms)==1 and "羽毛球" in terms[0]["value"]
    assert not get(client,headers,"/nodes",{**GROUP,"q":"羽毛球","confidence":.99})["items"]
    history=get(client,headers,"/timeline",{**GROUP,"view":"all"})["items"]
    assert all(a["at"]>=b["at"] for a,b in zip(history,history[1:]))
    assert {tuple(re.findall(r'"([^"]+)"',row["scope"]))[2] for row in history}=={"300","301","fishbot-v2"}
    role=get(client,headers,"/timeline",{**SYSTEM,"view":"autobiography"})["items"]
    assert role[0]["topic"]=="M2 启动" and role[0]["origin"]=="system"
    assert "改名" not in role[0]["value"]
    assert get(client,headers,"/self",DEMO)["open_goals"][0]["state"]=="proposed"


def test_m3_preview_no_state_change_no_impersonation_and_origin(workspace):
    client,store,headers=workspace
    before=get(client,headers,"")
    body={"scope":GROUP,"subject":"author-1","text":"你还记得我喜欢什么吗？"}
    res=client.post(M+"/preview",json=body,headers=headers)
    assert res.status_code==200,res.text
    data=res.json()
    assert data["count"] and "羽毛球" in data["items"][0]["memory"]["记忆"]
    assert data["items"][0]["sources"] and data["items"][0]["score"]>0
    assert 0<data["estimated_tokens_upper"]<=data["token_budget"]
    assert not data["other_conversations_queried"]
    assert client.post(M+"/preview",json={**body,"subject":"made-up"},headers=headers).status_code==403
    assert client.post(M+"/preview",json={**body,"scope":{**GROUP,"channel":"302"}},headers=headers).status_code==403
    assert client.post(M+"/preview",json=body,headers={**headers,"Origin":"https://example.invalid"}).status_code==403
    assert get(client,headers,"")==before
    with closing(store.open_read()) as db:
        assert db.execute("SELECT count(*) FROM outbox WHERE state='sent'").fetchone()[0]==0


def test_m3_forget_edit_optimistic_versions_and_audit(workspace):
    client,store,headers=workspace
    graph=get(client,headers,"/graph",GROUP)
    item=next(n for n in graph["nodes"] if "循环" in n["value"])
    id=item["id"]
    node=get(client,headers,f"/nodes/{id}",GROUP)
    meta={"scope":GROUP,"updated_at":node["updated_at"]}
    info=get(client,headers,f"/nodes/{id}/forget-impact",GROUP)
    assert info["reflections"]==1 and info["versions"]==1
    changed=client.put(M+f"/nodes/{id}/content",json={**meta,"value":"曾练习 Python 的循环变量。"},headers=headers)
    assert changed.status_code==200,changed.text
    new=changed.json()["id"]
    assert new!=id
    current=get(client,headers,f"/nodes/{new}",GROUP)
    assert any(r["action"]=="admin_edit" for r in current["revisions"])
    assert current["sources"]==node["sources"]
    assert not get(client,headers,"/nodes",{**GROUP,"node_kind":"reflection"})["items"]
    assert client.put(M+f"/nodes/{id}/content",json={**meta,"value":"另一条修订。"},headers=headers).status_code==409
    delete={"scope":GROUP,"updated_at":current["updated_at"],"confirmation":new}
    assert client.post(M+f"/nodes/{new}/forget",json={**delete,"confirmation":id},headers=headers).status_code==422
    assert client.post(M+f"/nodes/{new}/forget",json=delete,headers=headers).status_code==200
    assert not get(client,headers,"/nodes",{**GROUP,"q":"循环"})["items"]
    assert new not in {row["id"] for row in get(client,headers,"/graph",GROUP)["nodes"]}
    with closing(store.open_read()) as db:
        assert db.execute("SELECT count(*) FROM events WHERE platform='qq' AND channel_id='300'").fetchone()[0]==4
        assert db.execute("SELECT count(*) FROM memory_suppression").fetchone()[0]==1
        assert {r[0] for r in db.execute("SELECT action FROM panel_audit")} >= {"cognitive_edit","cognitive_forget"}


def test_m3_source_keeps_text_inert_scope_restricted_and_token_out_of_query(workspace):
    client,_,headers=workspace
    row=get(client,headers,"/nodes",{**GROUP,"q":"头像"})["items"][0]
    id=row["id"]
    src=get(client,headers,f"/nodes/{id}",GROUP)["sources"][0]["job_id"]
    route=f"/nodes/{id}/sources/{src}"
    assert MARKUP in get(client,headers,route,GROUP)["text"]
    assert client.get(M+route,params={**GROUP,"channel":"301"},headers=headers).status_code==404
    assert client.get(M+route,params={**GROUP,"channel":"302"},headers=headers).status_code==403
    assert client.get(M+route,params={**GROUP,"platform":"bilibili"},headers=headers).status_code==403
    assert client.get(M+"/nodes",params={**GROUP,"token":"test-only-noncredential"}).status_code==401
    assert client.get(M+"/nodes",params={**GROUP,"q":"%' OR 1=1 --"},headers=headers).json()["items"]==[]
    fake={"scope":{**GROUP,"channel":"301"},"updated_at":row["updated_at"],"value":"伪造修改。"}
    assert client.put(M+f"/nodes/{id}/content",json=fake,headers=headers).status_code==409
    assert client.get(M+"/nodes",params=GROUP,headers={**headers,"Origin":"https://wrong.invalid"}).status_code==403

