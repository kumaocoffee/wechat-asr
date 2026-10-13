'use strict';
const mod=Process.getModuleByName('Weixin.dll');
const base=mod.base;
function native(rva,ret,args){return new NativeFunction(base.add(rva),ret,args,{abi:'win64',exceptions:'steal'});}
const keep=[];
const alloc=native(0x71659ec,'pointer',['uint64']);
const free=native(0x7165a28,'void',['pointer']);
function emptyString(){const p=Memory.alloc(32);p.writeByteArray(new Uint8Array(32));p.add(24).writeU64(15);keep.push(p);return p;}
function assignString(p,bytes){
 const n=bytes.length;if(n<16){p.writeByteArray(bytes);p.add(n).writeU8(0);p.add(16).writeU64(n);p.add(24).writeU64(15);return;}
 const cap=n|15;let raw=alloc(cap+1+(cap+1>=4096?39:0));let data=raw;
 if(cap+1>=4096){data=raw.add(39).and(ptr('0xffffffffffffffe0'));data.sub(8).writePointer(raw);}
 data.writeByteArray(bytes);data.add(n).writeU8(0);p.writePointer(data);p.add(16).writeU64(n);p.add(24).writeU64(cap);
}
function buffer(bytes){const p=Memory.alloc(Math.max(bytes.length,1));if(bytes.length)p.writeByteArray(bytes);keep.push(p);return p;}
function readString(p){const n=p.add(16).readU64().toNumber();if(n>2000000)throw Error('string too large');const d=p.add(24).readU64().toNumber()<16?p:p.readPointer();return {bytes:Array.from(new Uint8Array(d.readByteArray(n))),length:n};}
function destroyString(p){const cap=p.add(24).readU64().toNumber();if(cap>=16){let d=p.readPointer(),n=cap+1;if(n>=4096){d=d.sub(8).readPointer();n+=39;}native(0x7165a30,'void',['pointer','uint64'])(d,n);}}
function encode(pcm){
 const encoder=alloc(0xe8),id=emptyString(),raw=buffer(pcm),out=Memory.alloc(40);
 native(0x615f9c0,'pointer',['pointer','int','uint8','pointer'])(encoder,16000,0,id);
 native(0x6160020,'void',['pointer','pointer','uint64'])(encoder,raw,pcm.length);
 native(0x615fe50,'pointer',['pointer','pointer'])(encoder,out);
 const data=readString(out.add(8));const frames=out.add(4).readU32();
 const failed=encoder.add(0xc5).readU8();
 send({event:'encoded',frames,bytes:data.length,failed});
 destroyString(out.add(8));native(0x615fa60,'pointer',['pointer','int'])(encoder,1);
 return {data,frames,failed};
}
let active=null;
let pending=null;
const coroutineHook=Interceptor.attach(base.add(0x462b70),{onLeave(ret){
 if(!pending)return;
 try{if(ret.isNull()||ret.readPointer().isNull())return;}catch(_){return;}
 const action=pending;pending=null;
 send({event:'coroutine_dispatch',tid:Process.getCurrentThreadId()});
 try{action();}catch(e){active.events.push({event:'dispatch_error',error:String(e),pc:e.context?.pc?.sub(base).toString()});active.done=true;send(active.events[active.events.length-1]);}
}});
const responseHook=Interceptor.attach(base.add(0x6905510),{onEnter(args){
 if(!active||!args[0].equals(active.session))return;
 const p=args[1];
 try{const event={event:'server_response',error:p.readU8(),finish:p.add(1).readU8(),seq:p.add(4).readS32(),text:textOf(p.add(8))};active.events.push(event);send(event);}catch(e){send({event:'response_inspect_error',error:String(e)});}
}});
async function onCoroutine(action){
 let done=false,error=null;
 pending=()=>{try{action();}catch(e){error=String(e);}finally{done=true;}};
 for(let i=0;i<300&&!done;i++)await new Promise(resolve=>setTimeout(resolve,100));
 if(!done){pending=null;throw Error('No coroutine dispatch within 30 seconds');}
 if(error)throw Error(error);
}
function destroySession(p){
 const strategy=p.add(8).readPointer();
 if(strategy.readPointer().equals(base.add(0x991de58)))strategy.add(0x50).readPointer().writeU8(1);
 p.writePointer(base.add(0x9854c28));
 native(0x6903d90,'void',['pointer'])(p);free(p);
}
function textOf(p){const s=readString(p);return p.add(24).readU64().toNumber()<16?p.readUtf8String(s.length):p.readPointer().readUtf8String(s.length);}
function createSession(id,scene,codec){
 const session=alloc(0x118),key=emptyString(),cfg=Memory.alloc(32),opts=Memory.alloc(1);
 cfg.writeByteArray(new Uint8Array(32));cfg.add(16).writeU32(codec);cfg.add(20).writeU8(1);opts.writeU8(0);
 if(!/^[\x20-\x7e]+$/.test(id))throw Error('session id must be ASCII');
 assignString(key,Array.from(id,c=>c.charCodeAt(0)));
 native(0x69031c0,'pointer',['pointer','int','pointer','pointer','int','pointer'])(session,Date.now()%0x7fffffff,key,cfg,scene,opts);
 destroyString(key);
 const vtable=alloc(64);vtable.writeByteArray(base.add(0x9854c28).readByteArray(64));
 const resultCb=new NativeCallback((self,text,seq,a,b,finish)=>{
  try {const event={event:'recognition',text:textOf(text),seq,a,b,finish};active.events.push(event);send(event);if(finish)active.done=true;}
  catch(e){send({event:'callback_error',error:String(e)});}
 },'void',['pointer','pointer','int','int','int','uint8'],'win64');
 const errorCb=new NativeCallback((self,flag,code,detail)=>{const event={event:'recognition_error',flag,code,detail};active.events.push(event);send(event);active.done=true;},'void',['pointer','uint8','int','int'],'win64');
 const noopCb=new NativeCallback(()=>{},'void',['pointer'],'win64');
 vtable.add(8).writePointer(resultCb);vtable.add(16).writePointer(errorCb);vtable.add(24).writePointer(noopCb);
 session.writePointer(vtable);keep.push(vtable,resultCb,errorCb,noopCb,cfg,opts);
 const strategy=session.add(8).readPointer();
 active={session,strategy,vtable,events:[],done:false,id};
 return {session:session.toString(),strategy:strategy.toString(),strategyVtable:strategy.readPointer().sub(base).toString(),id,scene,codec};
}
rpc.exports={
 info(){return {pid:Process.id,base:base.toString(),path:mod.path,size:mod.size,arch:Process.arch};},
 config(){const ilink=native(0x6903940,'uint8',['int']); const opus=native(0x46c2880,'int',[]);return {voice_input_ilink:ilink(36),voice_input_opus:opus()};},
 ready(){const kernel=native(0x425a0,'pointer',[])();return {kernel:!kernel.isNull(),account:!kernel.isNull()&&kernel.add(0x148).readU32()!==0};},
 read(rva,n){return Array.from(new Uint8Array(base.add(rva).readByteArray(n)));},
 vtable(rva,count){return Array.from({length:count},(_,i)=>base.add(rva+i*8).readPointer().sub(base).toString());},
 encode(pcm){return encode(pcm);},
 state(address){const p=ptr(address);return {final:p.add(0xc7).readU8(),stopping:p.add(0xc5).readU8(),text:textOf(p.add(0xc8)),seq:p.add(0x110).readS32(),extra:textOf(p.add(0xf0))};},
 async cleanup(addresses){await onCoroutine(()=>{for(const address of addresses)destroySession(ptr(address));});return {cleaned:addresses.length};},
 async finalize(){
  pending=null;if(!active)return {cleaned:true};
  const a=active;
  // Prevent any late iLink response from reaching JavaScript callbacks.
  if(a.strategy.readPointer().equals(base.add(0x991de58)))a.strategy.add(0x50).readPointer().writeU8(1);
  for(const offset of [8,16,24])a.vtable.add(offset).writePointer(base.add(0x83375));
  try{await onCoroutine(()=>{destroySession(a.session);free(a.vtable);});active=null;return {cleaned:true};}
  catch(e){return {cleaned:false,cancelled:true,error:String(e),note:'Native allocations retained until Weixin exits; callbacks replaced with a native return stub'};}
 },
 create(id,scene,codec){return createSession(id,scene,codec);},
 async recognize(pcm,id,scene=36,codec=5){
  if(active)throw Error('Only one session per agent instance');
  const enc=encode(pcm);if(enc.failed||!enc.data.length)throw Error('encoding failed');
  let created;
  send({event:'waiting_for_coroutine',timeout_seconds:30});
  await onCoroutine(()=>{
   const cfg=rpc.exports.config();
   if(cfg.voice_input_ilink!==1||cfg.voice_input_opus!==1)throw Error('This PoC requires a logged-in client with iLink and Opus enabled');
   created=createSession(id,scene,codec);send({event:'session_created',...created});
   const audio=emptyString();assignString(audio,enc.data.bytes);
   const method=active.strategy.readPointer().add(8).readPointer();
   try{new NativeFunction(method,'void',['pointer','pointer','int','uint8','int'],{abi:'win64',exceptions:'steal'})(active.strategy,audio,1,1,codec);}
   finally{destroyString(audio);}
   send({event:'request_submitted',bytes:enc.data.length,frames:enc.frames});
  });
  for(let i=0;i<600&&!active.done;i++){await new Promise(resolve=>setTimeout(resolve,100));if(active.session.add(0xc7).readU8()){const event={event:'final_result',text:textOf(active.session.add(0xc8)),a:active.session.add(0xe8).readS32(),b:active.session.add(0xec).readS32()};active.events.push(event);send(event);active.done=true;}}
  const snapshot={...created,bytes:enc.data.length,frames:enc.frames,done:active.done,events:active.events};
  pending=null;
  await new Promise(resolve=>setTimeout(resolve,1000));
  await onCoroutine(()=>{destroySession(active.session);free(active.vtable);});
  active=null;
  snapshot.cleaned=true;
  return snapshot;
 }
};
send({event:'loaded',pid:Process.id});
