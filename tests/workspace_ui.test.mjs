import assert from "node:assert/strict";
import {randomUUID} from "node:crypto";
import {readFileSync} from "node:fs";
import vm from "node:vm";

class Element {
  constructor(){this.listeners={};this.children=[];this.value="";this.textContent="";this.files=[];this.classList={toggle(){}};this.className=""}
  addEventListener(name,callback){this.listeners[name]=callback}
  append(...items){this.children.push(...items)}
  replaceChildren(){this.children=[];this.value=""}
  setAttribute(name,value){this[name]=value}
  removeAttribute(name){delete this[name]}
}
const html=readFileSync("src/rag_aaup/web/workspace.html","utf8");
const nodes=Object.fromEntries([...html.matchAll(/id="([^"]+)"/g)].map(match=>["#"+match[1],new Element()]));
nodes["#mode"].value="answer";
let tenant="A",releaseOld,asks=0,timeoutNext=false;
const json=value=>({ok:true,status:200,json:async()=>value});
const context=vm.createContext({
  document:{querySelector:id=>nodes[id],createElement:()=>new Element()},
  crypto:{randomUUID},AbortSignal,Error,URL,FormData,setInterval:()=>0,confirm:()=>false,
  fetch:async path=>{
    if(path==="/api/config")return json({multi_tenant:false});
    if(path==="/api/status")return json({tenant_id:tenant,generation_configured:true,request_timeout_seconds:70,assistant_name:tenant,organization_name:tenant,knowledge_mode:"organization"});
    if(path==="/api/workspace"){
      if(tenant==="A")return new Promise(resolve=>{releaseOld=()=>resolve(json({tenant_id:"A",directory:"PRIVATE_A",document_count:1,chunk_count:1,last_report:{failed:{}},processing:{}}))});
      return json({tenant_id:"B",directory:"PRIVATE_B",document_count:1,chunk_count:1,last_report:{failed:{}},processing:{}});
    }
    if(path==="/api/documents")return json([{document_id:1,source:"PRIVATE_"+tenant,url:"/api/documents/1/file",is_latest:true}]);
    if(path==="/api/ask"){
      asks++;if(timeoutNext)throw Object.assign(new Error("timeout"),{name:"TimeoutError"});
      if(asks===1)return {ok:false,status:503,json:async()=>({detail:"Provider failure"})};
      return json({tenant_id:"B",conversation_id:"new",answer:"Grounded [S1]",sources:[],provider:"gemini"});
    }
    throw new Error("Unexpected request: "+path);
  }
});
vm.runInContext(readFileSync("src/rag_aaup/web/workspace.js","utf8"),context);
await new Promise(resolve=>setImmediate(resolve));
assert.equal(typeof releaseOld,"function");
tenant="B";
await vm.runInContext('startWorkspace({tenant_id:"B",company_name:"B",username:"beta"})',context);
releaseOld();
await new Promise(resolve=>setImmediate(resolve));
const contents=node=>node.textContent+node.children.map(contents).join("");
assert.match(contents(nodes["#document-list"]),/PRIVATE_B/);
assert.doesNotMatch(contents(nodes["#document-list"]),/PRIVATE_A/);
assert.equal(nodes["#directory"].textContent,"PRIVATE_B");
nodes["#question"].value="Policy?";
const send=()=>nodes["#form"].listeners.submit({preventDefault(){}});
await send();assert.equal(nodes["#answer"].className,"error");assert.equal(nodes["#submit"].disabled,false);
await Promise.all([send(),send()]);assert.equal(asks,2);assert.equal(nodes["#answer"].className,"");
timeoutNext=true;await send();assert.match(nodes["#answer"].textContent,/انتهت مهلة/);assert.equal(nodes["#submit"].disabled,false);
vm.runInContext("showLogin()",context);
assert.equal(contents(nodes["#document-list"]),"");assert.equal(nodes["#directory"].textContent,"");
console.log("UI passed: stale-company responses discarded, private views cleared, duplicate send blocked, failures and timeout recover");
