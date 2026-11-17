const $=id=>document.getElementById(id);let doc=null,selected=null,timer=null;
function toast(text){$('toast').textContent=text;$('toast').style.display='block';setTimeout(()=>$('toast').style.display='none',4500)}
async function api(url,method='GET',data){let opts={method};if(data instanceof FormData)opts.body=data;else if(data!==undefined){opts.headers={'Content-Type':'application/json'};opts.body=JSON.stringify(data)}const r=await fetch(url,opts);const value=await r.json();if(!r.ok)throw Error(value.error||'请求失败');return value}
function bind(id,fn){$(id).onclick=async()=>{try{$(id).disabled=true;await fn()}catch(e){toast(e.message)}finally{$(id).disabled=false}}}
function end(){return Math.max(0,...doc.clips.map(c=>c.start+c.length))}
function esc(s){return String(s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]))}
async function list(){const items=await api('/motion/projects');$('projects').innerHTML='<option value="">选择工程</option>'+items.map(p=>`<option value="${p.id}">${esc(p.name)}</option>`).join('');if(doc)$('projects').value=doc.id}
async function load(value){doc=value;selected=doc.clips.some(c=>c.id===selected)?selected:doc.clips[0]?.id;$('title').textContent=doc.name;$('rev').textContent='REV '+doc.revision;$('summary').textContent=`${doc.sources.length}源 · ${doc.clips.length}片段 · ${doc.width}×${doc.height}`;for(const f of ['width','height','fps'])$(f).value=doc[f];$('background').value=JSON.stringify(doc.background);$('scrubber').max=Math.max(.01,end()-.0001);$('time').max=end();$('total').textContent='/ '+end().toFixed(2)+'s';$('time').value=Math.min(+$('time').value,Math.max(0,end()-.0001));$('scrubber').value=$('time').value;
$('sources').innerHTML=doc.sources.map(s=>`<div class="source"><img src="/motion/assets/${s.frames[0].asset}.png">${esc(s.name)}<br>${s.frames.length}帧 / ${s.duration.toFixed(2)}s<br><button data-source="${s.id}">加入时间轴</button></div>`).join('');$('sources').querySelectorAll('button').forEach(b=>b.onclick=()=>run(()=>api(`/motion/projects/${doc.id}/clips`,'POST',{revision:doc.revision,source:b.dataset.source,start:0,lane:doc.clips.length%4})));
$('replaceSource').innerHTML=doc.sources.map(s=>`<option value="${s.id}">${esc(s.name)}</option>`).join('');$('clipSelect').innerHTML=doc.clips.map((c,i)=>`<option value="${c.id}">片段${i+1} / 轨${c.lane}</option>`).join('');$('clipSelect').value=selected||'';const length=Math.max(end(),1);$('timeline').innerHTML=doc.clips.slice().sort((a,b)=>a.lane-b.lane).map(c=>`<div class="clipbar"><button class="${selected===c.id?'selected':''}" data-id="${c.id}" style="left:${c.start/length*100}%;width:${Math.max(2,c.length/length*100)}%">轨${c.lane} ${c.reverse?'←':'→'} ${c.length.toFixed(2)}s</button></div>`).join('');$('timeline').querySelectorAll('button').forEach(b=>b.onclick=()=>{selected=b.dataset.id;load(doc)});
$('captures').innerHTML=doc.captures.slice().reverse().map(c=>`<div class="capture">REV ${c.revision} · ${c.count}帧 · ${esc(c.created)}<img src="/motion/projects/${doc.id}/captures/${c.id}/contact.png"><a href="/motion/projects/${doc.id}/captures/${c.id}/bundle.zip">下载PNG序列与来源清单 ZIP</a></div>`).join('');editor();await preview();await list()}
function editor(){const c=doc?.clips.find(c=>c.id===selected);if(!c)return;for(const f of ['start','length','in','out','lane','opacity','blend','feather'])$(f).value=c[f];for(const f of ['reverse','enabled'])$(f).checked=c[f];$('transform').value=JSON.stringify(c.transform,null,2);$('mask').value=JSON.stringify(c.mask,null,2);trackEditor(c)}
async function preview(){if(!doc)return;$('empty').style.display='none';$('preview').style.display='block';const time=+$('time').value;$('preview').src=`/motion/projects/${doc.id}/preview.png?time=${time}&r=${doc.revision}`;const detail=await api(`/motion/projects/${doc.id}/inspect?time=${time}`);$('inspect').textContent=JSON.stringify(detail,null,2)}
async function run(fn){try{await load(await fn())}catch(e){toast(e.message)}}
async function action(name,values={}){if(!selected)throw Error('请选择片段');await load(await api(`/motion/projects/${doc.id}/clips/${selected}/${name}`,'POST',{revision:doc.revision,...values}))}
bind('demo',async()=>load(await api('/motion/demo','POST',{})));bind('new',async()=>load(await api('/motion/projects','POST',{name:prompt('工程名称','新的影像编排')||'新的影像编排'})));
$('projects').onchange=()=>{if($('projects').value)run(()=>api('/motion/projects/'+$('projects').value))};$('clipSelect').onchange=()=>{selected=$('clipSelect').value;editor()};
bind('import',async()=>{if(!doc)throw Error('请先创建工程');const data=new FormData();for(const file of $('upload').files)data.append('files',file);data.append('revision',doc.revision);data.append('duration',$('frameDuration').value);data.append('name','上传序列');await load(await api(`/motion/projects/${doc.id}/sources`,'POST',data))});
bind('replace',async()=>{const data=new FormData();if(!$('replaceFile').files[0])throw Error('请选择图像');data.append('file',$('replaceFile').files[0]);data.append('revision',doc.revision);await load(await api(`/motion/projects/${doc.id}/sources/${$('replaceSource').value}/frames/${$('replaceIndex').value}`,'POST',data))});
bind('settings',async()=>load(await api(`/motion/projects/${doc.id}`,'PATCH',{revision:doc.revision,width:+$('width').value,height:+$('height').value,fps:+$('fps').value,background:JSON.parse($('background').value)})));
bind('save',async()=>{let v={};for(const f of ['start','length','in','out','lane','opacity','feather'])v[f]=+$(f).value;for(const f of ['reverse','enabled'])v[f]=$(f).checked;v.blend=$('blend').value;for(const f of ['transform','mask'])v[f]=JSON.parse($(f).value);await action('edit',v)});
bind('split',()=>action('split',{time:+$('time').value}));bind('duplicate',()=>action('duplicate',{start:+$('start').value,lane:+$('lane').value+1}));bind('delete',()=>action('delete'));bind('ripple',()=>action('ripple-delete'));for(const a of ['undo','redo'])bind(a,()=>run(()=>api(`/motion/projects/${doc.id}/${a}`,'POST',{revision:doc.revision})));
bind('capture',async()=>{toast('正在渲染并冻结捕获…');await load(await api(`/motion/projects/${doc.id}/captures`,'POST',{revision:doc.revision}));toast('捕获已保存，可下载ZIP')});$('scrubber').oninput=()=>{$('time').value=$('scrubber').value;preview().catch(e=>toast(e.message))};$('time').onchange=()=>{$('scrubber').value=$('time').value;preview().catch(e=>toast(e.message))};bind('play',()=>{if(timer){clearInterval(timer);timer=null;$('play').textContent='播放';return}if(!doc)return;timer=setInterval(()=>{let time=+$('time').value+1/doc.fps;if(time>=end())time=0;$('time').value=time.toFixed(4);$('scrubber').value=time;preview().catch(()=>{})},1000/doc.fps);$('play').textContent='暂停'});list().catch(e=>toast(e.message));

/* ===== 遮罩跟踪：源帧取帧、圈选锚点、跟踪与可靠性可视化 ===== */
const track={roi:[],closed:false,dragIndex:-1,frameIndex:0,frames:[],sourceId:null};
const canvas=$('trackCanvas'),ctx=canvas.getContext('2d');
function selectedClip(){return doc?.clips.find(c=>c.id===selected)||null}
function clipSource(c){return doc.sources.find(s=>s.id===c.source)}
function frameStartTimes(src){let t=0;return src.frames.map(f=>{const s=t;t+=f.duration;return s})}
function frameIndexAt(src,seconds){const starts=frameStartTimes(src);let idx=0;starts.forEach((s,i)=>{if(s<=seconds+1e-9)idx=i});return idx}
function trackEditor(c){
  if(!c){$('trackPanel').style.opacity=.4;return}
  $('trackPanel').style.opacity=1;
  const prevSource=track.sourceId;
  const src=clipSource(c);track.sourceId=src.id;track.frames=src.frames;
  const starts=frameStartTimes(src);
  const first=frameIndexAt(src,c.in),last=Math.min(src.frames.length-1,frameIndexAt(src,Math.max(c.in,c.out-1e-9)));
  $('trackFirst').value=first;$('trackLast').value=last;
  const sel=$('trackFrame');
  sel.innerHTML=src.frames.map((f,i)=>`<option value="${i}">帧${i} · ${starts[i].toFixed(2)}s</option>`).join('');
  if(prevSource!==src.id){track.roi=[];track.frameIndex=first}
  track.frameIndex=Math.min(Math.max(track.frameIndex,first),last);
  sel.value=track.frameIndex;
  drawTrackCanvas();renderReliability(c);
}
function drawTrackCanvas(){
  const c=selectedClip();if(!c)return;const src=clipSource(c);
  const frame=src.frames[track.frameIndex];
  const wantedFrame=track.frameIndex,wantedSource=src.id;
  const img=new Image();
  img.onload=()=>{
    // Ignore late loads after the user switched frame or clip.
    if(track.frameIndex!==wantedFrame||track.sourceId!==wantedSource)return;
    ctx.clearRect(0,0,canvas.width,canvas.height);
    ctx.drawImage(img,0,0,canvas.width,canvas.height);
    if(track.roi.length){
      ctx.beginPath();
      track.roi.forEach((p,i)=>{const x=p[0]*canvas.width,y=p[1]*canvas.height;i?ctx.lineTo(x,y):ctx.moveTo(x,y)});
      if(track.closed)ctx.closePath();
      ctx.fillStyle='rgba(110,217,190,.18)';ctx.fill();
      ctx.strokeStyle='#6cd9be';ctx.lineWidth=1.5;ctx.stroke();
      track.roi.forEach((p,i)=>{ctx.beginPath();ctx.arc(p[0]*canvas.width,p[1]*canvas.height,4,0,7);ctx.fillStyle=i===track.dragIndex?'#ffd479':'#6cd9be';ctx.fill()});
    }
  };
  img.src=`/motion/assets/${frame.asset}.png`;
}
function renderReliability(c){
  const box=$('reliability');const report=c.tracking?.frames||[];
  const staleNote=c.tracking?.stale?'<div class="stale-note">源帧已在跟踪后替换，可靠性结果可能过期，请重新跟踪</div>':'';
  if(!report.length){box.innerHTML=staleNote+'<span class="hint">尚无跟踪结果：圈定区域并点击“从锚点跟踪”</span>';return}
  const byFrame=Object.fromEntries(report.map(r=>[r.frame,r]));
  const first=+$('trackFirst').value,last=+$('trackLast').value;
  box.innerHTML=staleNote;
  const strip=document.createElement('div');strip.className='reliability';
  for(let i=first;i<=last;i++){
    const r=byFrame[i]||{state:'untracked'};
    const cell=document.createElement('div');
    cell.className='cell '+r.state+(i===track.frameIndex?' current':'');
    cell.textContent=i;cell.title=`帧${i} · ${stateLabel(r.state)}`+(r.confidence!=null?` · 置信度${r.confidence}`:'')+(r.error!=null?` · 误差${r.error}`:'');
    cell.onclick=()=>{track.frameIndex=i;$('trackFrame').value=i;drawTrackCanvas();renderReliability(c)};
    strip.appendChild(cell);
  }
  box.appendChild(strip);
}
function stateLabel(s){return {anchor:'手工锚点',reliable:'可靠',marginal:'存疑（纹理弱）',failed:'丢失/遮挡',untracked:'未跟踪'}[s]||s}
$('trackFrame').onchange=e=>{track.frameIndex=+e.target.value;drawTrackCanvas();const c=selectedClip();if(c)renderReliability(c)};
function canvasPos(e){const r=canvas.getBoundingClientRect();return [Math.min(1,Math.max(0,(e.clientX-r.left)/r.width)),Math.min(1,Math.max(0,(e.clientY-r.top)/r.height))]}
function hitVertex(p){for(let i=0;i<track.roi.length;i++){const q=track.roi[i];if(Math.hypot((p[0]-q[0])*canvas.width,(p[1]-q[1])*canvas.height)<9)return i}return -1}
canvas.addEventListener('click',e=>{
  if(track.closed)return;
  const p=canvasPos(e);
  if(track.roi.length>=3){const h=hitVertex(p);if(h===0){track.closed=true;drawTrackCanvas();return}}
  if(track.roi.length>=64){toast('单个遮罩最多64个顶点');return}
  track.roi.push(p);drawTrackCanvas();
});
canvas.addEventListener('dblclick',()=>{if(track.roi.length>=3&&!track.closed){track.closed=true;drawTrackCanvas()}});
canvas.addEventListener('mousedown',e=>{if(!track.closed)return;track.dragIndex=hitVertex(canvasPos(e))});
canvas.addEventListener('mousemove',e=>{if(track.dragIndex<0)return;track.roi[track.dragIndex]=canvasPos(e);drawTrackCanvas()});
window.addEventListener('mouseup',()=>track.dragIndex=-1);
$('clearRoi').onclick=()=>{track.roi=[];track.closed=false;drawTrackCanvas()};
$('useMask').onclick=()=>{
  const c=selectedClip();if(!c)return;const src=clipSource(c);
  const t=frameStartTimes(src)[track.frameIndex];
  const nearest=c.mask.reduce((a,b)=>Math.abs(b.time-t)<Math.abs(a.time-t)?b:a,c.mask[0]);
  if(!nearest){toast('该片段还没有遮罩关键帧');return}
  track.roi=nearest.points.map(p=>[...p]);track.closed=true;drawTrackCanvas();
};
$('setAnchor').onclick=async()=>{
  const c=selectedClip();if(!c)throw Error('请选择片段');
  if(!track.closed||track.roi.length<3)throw Error('请先在源帧上圈定闭合区域（至少3个顶点）');
  const src=clipSource(c);const t=frameStartTimes(src)[track.frameIndex];
  const mask=c.mask.filter(k=>Math.abs(k.time-t)>1e-9);
  mask.push({time:+t.toFixed(5),points:track.roi.map(p=>[...p]),origin:'manual'});
  mask.sort((a,b)=>a.time-b.time);
  await action('edit',{mask});
  toast(`已在帧${track.frameIndex}（${t.toFixed(2)}s）保存手工锚点`);
};
bind('track',async()=>{
  const c=selectedClip();if(!c)throw Error('请选择片段');
  const first=+$('trackFirst').value,last=+$('trackLast').value;
  if(last<first)throw Error('止帧不能早于起帧');
  toast('正在跟踪源帧…');
  await load(await api(`/motion/projects/${doc.id}/clips/${selected}/track`,'POST',{revision:doc.revision,first,last}));
  const updated=selectedClip();
  const failed=(updated.tracking?.frames||[]).filter(r=>r.state==='failed').length;
  const marginal=(updated.tracking?.frames||[]).filter(r=>r.state==='marginal').length;
  toast(failed?`跟踪在 ${failed} 帧丢失/遮挡处停止（另有 ${marginal} 帧存疑），可加手工锚点后重新跟踪`:'跟踪完成，可在遮罩关键帧文本框继续手工校正');
});
