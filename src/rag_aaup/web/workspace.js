const $=selector=>document.querySelector(selector);
let viewEpoch=0,activeTenantId=null;
let authenticated=false,multiTenant=false,registerMode=false,pending=false,fileOperation=false,workspacePending=false;
let conversationId=crypto.randomUUID(),generationReady=false,preferredMode=null,requestTimeout=70000;
const form=$("#form"),button=$("#submit"),mode=$("#mode");
async function jsonRequest(path,options={}){
  const requestEpoch=viewEpoch;
  const response=await fetch(path,{cache:"no-store",signal:AbortSignal.timeout(10000),...options});
  const data=await response.json();
  if(response.status===401&&multiTenant){if(requestEpoch===viewEpoch)showLogin();throw new Error("انتهت الجلسة. سجّل الدخول مجدداً.")}
  if(data.tenant_id&&activeTenantId&&data.tenant_id!==activeTenantId&&requestEpoch===viewEpoch){showLogin();throw new Error("تغيرت جلسة الشركة. سجّل الدخول مجدداً.")}
  if(!response.ok)throw new Error(typeof data.detail==="string"?data.detail:"تعذر إكمال الطلب");
  return data;
}
function clearPrivateView(){viewEpoch++;activeTenantId=null;workspacePending=false;pending=false;fileOperation=false;$("#document-scope").replaceChildren();const all=document.createElement("option");all.value="";all.textContent="كل ملفات شركتي";$("#document-scope").append(all);$("#directory").textContent="";$("#file-message").textContent="";$("#sync-state").textContent="";$("#files").value="";$("#selected-files").textContent="الملفات تبقى ضمن مساحة شركتك";$("#upload-state").hidden=true;$("#upload-label").textContent="";$("#organization-name").textContent="مساحتك الخاصة";$("#app-name").textContent="CampusIQ";for(const id of ["#upload","#reindex","#clear-all","#academic-import"])$(id).disabled=false;conversationId=crypto.randomUUID();$("#sources").replaceChildren();$("#answer").textContent="";$("#question").value="";$("#result").hidden=true;$("#document-list").replaceChildren();$("#document-count").textContent="—";$("#chunk-count").textContent="—"}
function showLogin(){authenticated=false;clearPrivateView();$("#app-shell").hidden=true;$("#auth-panel").hidden=false;$("#logout").hidden=true;$("#identity").textContent="مساحة معرفة خاصة بكل شركة"}
async function startWorkspace(principal){clearPrivateView();activeTenantId=principal?.tenant_id||null;authenticated=true;$("#auth-panel").hidden=true;$("#app-shell").hidden=false;$("#logout").hidden=!multiTenant;$("#password").value="";if(principal)$("#identity").textContent=principal.company_name+" · "+principal.username;await Promise.all([refreshStatus(),refreshWorkspace()])}
$("#auth-toggle").addEventListener("click",()=>{registerMode=!registerMode;$("#register-fields").hidden=!registerMode;$("#company-name").required=registerMode;$("#auth-title").textContent=registerMode?"إنشاء مساحة شركة مستقلة":"الدخول إلى مساحة شركتك";$("#auth-submit").textContent=registerMode?"إنشاء الحساب":"تسجيل الدخول";$("#auth-toggle").textContent=registerMode?"لدي حساب بالفعل":"إنشاء حساب شركة جديدة";$("#auth-message").textContent=""});
$("#auth-form").addEventListener("submit",async event=>{event.preventDefault();const submit=$("#auth-submit");submit.disabled=true;$("#auth-message").textContent="";try{const body={username:$("#username").value.trim(),password:$("#password").value};if(registerMode){body.company_name=$("#company-name").value.trim();body.knowledge_mode=$("#knowledge-mode").value}const principal=await jsonRequest(registerMode?"/api/auth/register":"/api/auth/login",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(body)});await startWorkspace(principal)}catch(error){$("#auth-message").textContent=error.message}finally{submit.disabled=false}});
$("#logout").addEventListener("click",async()=>{if(pending||fileOperation)return;try{await jsonRequest("/api/auth/logout",{method:"POST"})}finally{showLogin()}});
function showTab(name){$("#documents-pane").hidden=name!=="documents";$("#chat-pane").hidden=name!=="chat";for(const tab of ["documents","chat"])$("#tab-"+tab).setAttribute("aria-selected",String(name===tab))}
$("#tab-documents").addEventListener("click",()=>showTab("documents"));$("#tab-chat").addEventListener("click",()=>showTab("chat"));
function updateButton(){button.disabled=pending;button.textContent=pending?"جارٍ البحث...":mode.value==="sources"?"ابحث في الملفات":"اسأل"}
async function refreshStatus(){
  const epoch=viewEpoch;
  const state=await jsonRequest("/api/status");if(epoch!==viewEpoch||!authenticated)return;const wasReady=generationReady;generationReady=state.generation_configured;requestTimeout=state.request_timeout_seconds*1000;
  document.title=state.assistant_name+" | CampusIQ";$("#app-name").textContent=state.assistant_name;$("#organization-name").textContent=state.organization_name;
  $("#academic-import").hidden=!multiTenant||state.knowledge_mode!=="academic";
  if(!generationReady)mode.value="sources";else if(!wasReady&&preferredMode!=="sources")mode.value="answer";
  $("#setup").textContent=generationReady?"الإجابات من الملفات المحلية المفهرسة، مع إحالات تفتح المستند الأصلي.":"مزود التوليد غير مهيأ. يمكنك الآن البحث في مقتطفات الملفات؛ تُقرأ إعدادات المزود الجديدة تلقائياً.";updateButton();
}
mode.addEventListener("change",()=>{preferredMode=mode.value;updateButton()});
function renderSources(data){
  const container=$("#sources");container.replaceChildren();
  for(const source of data.sources){const item=document.createElement("div");item.className="source";const title=document.createElement("strong");title.textContent=`[${source.citation}] ${source.source}`;item.append(title);
    if(source.url&&source.url.startsWith("/api/documents/")){const link=document.createElement("a");link.href=source.url;link.target="_blank";link.rel="noopener noreferrer";link.textContent="فتح المرجع المحلي";link.style.display="block";link.style.marginTop="9px";item.append(link)}
    const details=document.createElement("details"),summary=document.createElement("summary"),excerpt=document.createElement("pre");summary.textContent="عرض الدليل المسترجع";excerpt.textContent=source.text;details.open=data.provider==="sources_only";details.append(summary,excerpt);item.append(details);container.append(item)
  }
}
form.addEventListener("submit",async event=>{
  event.preventDefault();if(pending||!authenticated)return;pending=true;const epoch=viewEpoch;updateButton();$("#result").hidden=false;$("#answer").className="";$("#answer").textContent="";$("#sources").replaceChildren();
  try{await refreshStatus();if(epoch!==viewEpoch||!authenticated)return;const data=await jsonRequest("/api/ask",{method:"POST",signal:AbortSignal.timeout(requestTimeout+5000),headers:{"Content-Type":"application/json"},body:JSON.stringify({question:$("#question").value,conversation_id:conversationId,mode:mode.value,document_id:$("#document-scope").value?Number($("#document-scope").value):null})});if(epoch!==viewEpoch||!authenticated)return;conversationId=data.conversation_id;$("#answer").textContent=data.answer;renderSources(data)}
  catch(error){if(epoch!==viewEpoch)return;$("#answer").className="error";$("#answer").textContent=["TimeoutError","AbortError"].includes(error.name)?"انتهت مهلة الطلب. يمكنك المحاولة مجدداً أو اختيار المقتطفات فقط.":error.message}
  finally{if(epoch===viewEpoch){pending=false;updateButton()}}
});
$("#new-chat").addEventListener("click",()=>{if(!pending){conversationId=crypto.randomUUID();$("#question").value="";$("#result").hidden=true;$("#sources").replaceChildren()}});
async function refreshWorkspace(){
  if(workspacePending||!authenticated)return;workspacePending=true;const epoch=viewEpoch;
  try{const [state,documents]=await Promise.all([jsonRequest("/api/workspace"),jsonRequest("/api/documents")]);if(epoch!==viewEpoch||!authenticated)return;$("#directory").textContent=state.directory;$("#document-count").textContent=state.document_count;$("#chunk-count").textContent=state.chunk_count;
    const failures=Object.entries(state.last_report?.failed||{}),progress=state.processing;
    $("#sync-state").className=failures.length?"error":"muted small";$("#sync-state").textContent=state.is_syncing?`جارٍ فهرسة ${progress.file||"الملفات"} · ${progress.completed}/${progress.total}`:failures.length?failures.map(([name,error])=>`${name}: ${error}`).join(" | "):state.document_count?`الفهرس جاهز · ${documents.length} مستند · فحص كل ${state.refresh_interval_seconds} ثانية`:"لا توجد ملفات بعد. ارفع المستندات التي تريد البحث فيها.";
    if(fileOperation&&state.is_syncing){$("#upload-label").textContent=$("#sync-state").textContent;$("#upload-progress").value=progress.total?100*progress.completed/progress.total:0}
    const scope=$("#document-scope"),selected=scope.value;scope.replaceChildren();const all=document.createElement("option");all.value="";all.textContent="كل ملفات شركتي";scope.append(all);for(const entry of documents){const option=document.createElement("option");option.value=String(entry.document_id);option.textContent=entry.source;scope.append(option)}if(documents.some(entry=>String(entry.document_id)===selected))scope.value=selected;
    const list=$("#document-list");list.replaceChildren();for(const entry of documents){const row=document.createElement("div"),content=document.createElement("div"),link=document.createElement("a"),small=document.createElement("small"),remove=document.createElement("button");row.className="file-row";link.href=entry.url;link.target="_blank";link.rel="noopener noreferrer";link.textContent=entry.source;small.textContent="مفهرس وجاهز للبحث · "+(entry.is_latest?"النسخة الحالية":"مرجع أقدم");remove.className="ghost";remove.textContent="إزالة";remove.disabled=fileOperation;remove.addEventListener("click",()=>removeDocument(entry.document_id));content.append(link,small);row.append(content,remove);list.append(row)}
  }finally{if(epoch===viewEpoch)workspacePending=false}
}
function selectedFiles(){const files=[...$("#files").files];$("#selected-files").textContent=files.length?files.map(file=>file.name).join("، "):"الملفات تبقى ضمن مساحة شركتك"}
$("#files").addEventListener("change",selectedFiles);$("#dropzone").addEventListener("click",()=>$("#files").click());$("#dropzone").addEventListener("keydown",event=>{if(event.key==="Enter"||event.key===" "){event.preventDefault();$("#files").click()}});
for(const name of ["dragover","dragleave","drop"])$("#dropzone").addEventListener(name,event=>{event.preventDefault();$("#dropzone").classList.toggle("drag",name==="dragover");if(name==="drop"){$("#files").files=event.dataTransfer.files;selectedFiles()}});
function uploadRequest(body){const epoch=viewEpoch;return new Promise((resolve,reject)=>{const xhr=new XMLHttpRequest();xhr.open("POST","/api/documents");xhr.timeout=300000;xhr.upload.onprogress=event=>{if(epoch!==viewEpoch)return;if(event.lengthComputable){$("#upload-progress").value=100*event.loaded/event.total;$("#upload-label").textContent=event.loaded===event.total?"اكتمل الرفع، جارٍ استخراج الملفات وفهرستها...":`رفع الملفات: ${Math.round(100*event.loaded/event.total)}%`}};xhr.onload=()=>{try{if(epoch!==viewEpoch)throw new Error("Workspace changed");const data=JSON.parse(xhr.responseText);if(xhr.status===401){showLogin();throw new Error("سجّل الدخول مجدداً")}if(xhr.status>=400)throw new Error(typeof data.detail==="string"?data.detail:"تعذر رفع الملفات");resolve(data)}catch(error){reject(error)}};xhr.onerror=()=>reject(new Error("تعذر الاتصال بالخادم"));xhr.ontimeout=()=>reject(new Error("انتهت مهلة الرفع. تابع حالة الفهرسة ثم أعد المحاولة."));xhr.send(body)})}
async function fileAction(action){if(fileOperation||!authenticated)return;fileOperation=true;const epoch=viewEpoch;for(const id of ["#upload","#reindex","#clear-all","#academic-import"])$(id).disabled=true;$("#file-message").className="";$("#file-message").textContent="جارٍ المعالجة...";$("#upload-state").hidden=false;$("#upload-progress").removeAttribute("value");
  try{const result=await action();if(epoch!==viewEpoch)return;if(Object.keys(result.failed||{}).length)throw new Error(Object.entries(result.failed).map(([name,error])=>`${name}: ${error}`).join(" | "));$("#upload-progress").value=100;$("#upload-label").textContent="اكتملت المعالجة";$("#file-message").className="success";$("#file-message").textContent=`تم: ${(result.added||[]).length} جديد، ${(result.updated||[]).length} محدّث، ${(result.skipped_duplicates||[]).length} مكرر متخطى، ${(result.removed||[]).length} أزيل من الفهرس.`}
  catch(error){if(epoch!==viewEpoch)return;$("#file-message").className="error";$("#file-message").textContent=error.message;$("#upload-state").hidden=true}
  finally{if(epoch===viewEpoch){fileOperation=false;for(const id of ["#upload","#reindex","#clear-all","#academic-import"])$(id).disabled=false;await refreshWorkspace()}}
}
$("#upload").addEventListener("click",()=>fileAction(async()=>{if(!$("#files").files.length)throw new Error("اختر ملفاً أولاً");const body=new FormData();for(const file of $("#files").files)body.append("files",file);const result=await uploadRequest(body);$("#files").value="";selectedFiles();return result}));
$("#reindex").addEventListener("click",()=>fileAction(()=>jsonRequest("/api/reindex",{method:"POST",signal:AbortSignal.timeout(300000)})));
$("#clear-all").addEventListener("click",()=>{if(confirm("إزالة جميع ملفات هذه الشركة من قاعدة المعرفة ونقلها إلى سلة الاستعادة؟"))fileAction(()=>jsonRequest("/api/documents",{method:"DELETE",headers:{"Content-Type":"application/json"},body:JSON.stringify({confirmation:"DELETE"}),signal:AbortSignal.timeout(120000)}))});
function removeDocument(id){if(confirm("إزالة هذا الملف من مساحة شركتك ونقله إلى سلة الاستعادة؟"))fileAction(()=>jsonRequest(`/api/documents/${id}`,{method:"DELETE"}))}
$("#academic-import").addEventListener("click",()=>fileAction(()=>jsonRequest("/api/academic/import",{method:"POST",signal:AbortSignal.timeout(300000)})));
setInterval(()=>refreshWorkspace().catch(()=>{}),2000);
(async()=>{try{const config=await jsonRequest("/api/config");multiTenant=config.multi_tenant;if(!multiTenant){await startWorkspace(null);return}try{const principal=await jsonRequest("/api/auth/me");await startWorkspace(principal)}catch{showLogin()}}catch(error){showLogin();$("#auth-message").textContent=error.message}})();

$("#document-scope").addEventListener("change",()=>{conversationId=crypto.randomUUID();$("#result").hidden=true});
