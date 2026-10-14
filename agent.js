'use strict';
const mod=Process.getModuleByName('Weixin.dll');
const base=mod.base;
function native(rva,ret,args){return new NativeFunction(base.add(rva),ret,args,{abi:'win64',exceptions:'steal'});}
const alloc=native(0x71659ec,'pointer',['uint64']);
const free=native(0x7165a28,'void',['pointer']);
function emptyString(keep){const p=Memory.alloc(32);p.writeByteArray(new Uint8Array(32));p.add(24).writeU64(15);keep.push(p);return p;}
function assignString(p,bytes){
 const n=bytes.length;if(n<16){p.writeByteArray(bytes);p.add(n).writeU8(0);p.add(16).writeU64(n);p.add(24).writeU64(15);return;}
 const cap=n|15;let raw=alloc(cap+1+(cap+1>=4096?39:0));let data=raw;
 if(cap+1>=4096){data=raw.add(39).and(ptr('0xffffffffffffffe0'));data.sub(8).writePointer(raw);}
 data.writeByteArray(bytes);data.add(n).writeU8(0);p.writePointer(data);p.add(16).writeU64(n);p.add(24).writeU64(cap);
}
function buffer(bytes,keep){const p=Memory.alloc(Math.max(bytes.length,1));if(bytes.length)p.writeByteArray(bytes);keep.push(p);return p;}
function readString(p){const n=p.add(16).readU64().toNumber();if(n>2000000)throw Error('string too large');const d=p.add(24).readU64().toNumber()<16?p:p.readPointer();return {bytes:Array.from(new Uint8Array(d.readByteArray(n))),length:n};}
function destroyString(p){const cap=p.add(24).readU64().toNumber();if(cap>=16){let d=p.readPointer(),n=cap+1;if(n>=4096){d=d.sub(8).readPointer();n+=39;}native(0x7165a30,'void',['pointer','uint64'])(d,n);}}
function encode(pcm,keep=[]){
 const encoder=alloc(0xe8),id=emptyString(keep),raw=buffer(pcm,keep),out=Memory.alloc(40);
 native(0x615f9c0,'pointer',['pointer','int','uint8','pointer'])(encoder,16000,0,id);
 native(0x6160020,'void',['pointer','pointer','uint64'])(encoder,raw,pcm.length);
 native(0x615fe50,'pointer',['pointer','pointer'])(encoder,out);
 const data=readString(out.add(8));const frames=out.add(4).readU32();
 const failed=encoder.add(0xc5).readU8();
 send({event:'encoded',frames,bytes:data.length,failed});
 destroyString(out.add(8));native(0x615fa60,'pointer',['pointer','int'])(encoder,1);
 return {data,frames,failed};
}
function opusPackets(bytes,frames){
 // Verified native format: a 16-byte header, then uint16-LE length + Opus frame.
 const magic='#!OPUS_RAW_V1';
 if(bytes.length<16||!Array.from(magic,(c,i)=>bytes[i]===c.charCodeAt(0)).every(Boolean))throw Error('Unexpected Opus header');
 const bounds=[16];let pos=16;
 while(pos<bytes.length){
  if(pos+2>bytes.length)throw Error('Incomplete Opus frame length');
  const n=bytes[pos]|(bytes[pos+1]<<8);pos+=2+n;
  if(!n||pos>bytes.length)throw Error('Incomplete Opus frame');
  bounds.push(pos);
 }
 if(bounds.length-1!==frames)throw Error('Opus frame count mismatch');
 const packets=[];
 // 250 frames = 5 seconds per packet. Preserve encoder state and all audio.
 // The header belongs to the stream and is included only in the first packet.
 for(let f=0;f<frames;f+=250){
  const end=Math.min(f+250,frames);
  packets.push(bytes.slice(f===0?0:bounds[f],bounds[end]));
 }
 return packets;
}
const requests=new Map();
const sessions=new Map();
const pending=[];
let sequence=Date.now()%0x7fffffff;
let inFlight=0;
let cleanupFailed=false;
let disposing=false;
let disposed=false;
let dispatching=false;
// Native objects must never contain pointers into a disposable JS runtime.
// VirtualAlloc and the Weixin allocator are process-owned, unlike Memory.alloc
// and NativeCallback. Keep this tiny fallback table/page until Weixin exits so
// host termination and automatic Frida unload leave callable native no-ops.
const kernel32=Process.getModuleByName('kernel32.dll');
const virtualAlloc=new NativeFunction(kernel32.getExportByName('VirtualAlloc'),'pointer',['pointer','uint64','uint32','uint32'],{abi:'win64'});
const callbackCode=virtualAlloc(ptr(0),4096,0x3000,0x04);
if(callbackCode.isNull())throw Error('Cannot allocate native callback fallbacks');
callbackCode.writeByteArray(new Uint8Array(64).fill(0x90));
for(const offset of [0,16,32])callbackCode.add(offset).writeByteArray([0x31,0xc0,0x90,0x90,0x90,0xc3]);
if(!Memory.protect(callbackCode,4096,'r-x'))throw Error('Cannot protect native callback fallbacks');
const callbackVtable=alloc(64);
callbackVtable.writeByteArray(base.add(0x9854c28).readByteArray(64));
function acceptsCallback(self){const context=sessions.get(self.toString());return context&&!context.closing?context:null;}
const resultHook=Interceptor.attach(callbackCode,{onEnter(args){
 const context=acceptsCallback(args[0]);if(disposed||!context)return;
 try{context.events.push({event:'recognition',text:textOf(args[1]),seq:args[2].toInt32(),a:args[3].toInt32(),b:args[4].toInt32(),finish:args[5].toInt32()&0xff});}
 catch(e){send({event:'callback_error',error:String(e)});}
}});
const errorHook=Interceptor.attach(callbackCode.add(16),{onEnter(args){
 const context=acceptsCallback(args[0]);if(disposed||!context)return;
 context.events.push({event:'recognition_error',flag:args[1].toInt32()&0xff,code:args[2].toInt32(),detail:args[3].toInt32()});context.done=true;
}});
callbackVtable.add(8).writePointer(callbackCode);callbackVtable.add(16).writePointer(callbackCode.add(16));callbackVtable.add(24).writePointer(callbackCode.add(32));
const coroutineHook=Interceptor.attach(base.add(0x462b70),{onLeave(ret){
 if(disposed||dispatching||!pending.length)return;
 try{if(ret.isNull()||ret.readPointer().isNull())return;}catch(_){return;}
 const job=pending.shift();clearTimeout(job.timer);
 send({event:'coroutine_dispatch',tid:Process.getCurrentThreadId()});
 dispatching=true;
 try{job.action();job.resolve();}catch(e){job.reject(e);}finally{dispatching=false;}
}});
const responseHook=Interceptor.attach(base.add(0x6905510),{onEnter(args){
 const context=acceptsCallback(args[0]);if(disposed||!context)return;
 const p=args[1];
 try{const event={event:'server_response',error:p.readU8(),finish:p.add(1).readU8(),seq:p.add(4).readS32(),text:textOf(p.add(8))};context.events.push(event);send({...event,id:context.id});}catch(e){send({event:'response_inspect_error',error:String(e)});}
}});
async function onCoroutine(action){
 if(disposed)throw Error('Agent is disposed');
 return new Promise((resolve,reject)=>{
  const job={action,resolve,reject,timer:null};
  job.timer=setTimeout(()=>{
   const index=pending.indexOf(job);
   if(index!==-1){pending.splice(index,1);reject(Error('No coroutine dispatch within 30 seconds'));}
  },30000);
  pending.push(job);
 });
}
function destroySession(p){
 p.writePointer(base.add(0x9854c28));
 native(0x6903d90,'void',['pointer'])(p);free(p);
}
function nativeTaskReferences(context){
 if(!context.stopControl||context.stopControl.isNull())throw Error('Missing native cancellation ownership');
 const refs=context.stopControl.add(8).readS32();
 if(refs<1)throw Error('Invalid native cancellation reference count');
 return refs;
}
async function drainNativeTasks(context){
 // The verified iLink strategy owns one cancellation shared_ptr. Each queued
 // native response closure copies it; destruction releases those references.
 // Stop first to unregister the network callback and prevent new producers.
 await onCoroutine(()=>{
  if(!context.strategy.readPointer().equals(base.add(0x991de58)))throw Error('Unsupported native cancellation strategy');
  context.stopControl=context.strategy.add(0x58).readPointer();
  nativeTaskReferences(context);
  native(0x6ea1fe0,'void',['pointer'])(context.strategy);
  context.nativeStopped=true;
 });
 const started=Date.now();
 let refs=nativeTaskReferences(context);
 send({event:'native_draining',id:context.id,references:refs});
 while(refs!==1){
  if(Date.now()-started>=30000)throw Error('Native tasks did not release cancellation ownership within 30 seconds');
  await new Promise(resolve=>setTimeout(resolve,50));
  refs=nativeTaskReferences(context);
 }
 send({event:'native_drained',id:context.id,references:refs,elapsed_ms:Date.now()-started});
}
function textOf(p){const s=readString(p);return p.add(24).readU64().toNumber()<16?p.readUtf8String(s.length):p.readPointer().readUtf8String(s.length);}
function createSession(context,scene,codec){
 const id=context.id,session=alloc(0x118),key=emptyString(context.keep),cfg=Memory.alloc(32),opts=Memory.alloc(1);
 cfg.writeByteArray(new Uint8Array(32));cfg.add(16).writeU32(codec);cfg.add(20).writeU8(1);opts.writeU8(0);
 if(!/^[\x20-\x7e]+$/.test(id))throw Error('session id must be ASCII');
 assignString(key,Array.from(id,c=>c.charCodeAt(0)));
 context.session=session;context.constructed=false;context.keep.push(cfg,opts);
 sequence=(sequence+1)%0x7fffffff;
 try{native(0x69031c0,'pointer',['pointer','int','pointer','pointer','int','pointer'])(session,sequence,key,cfg,scene,opts);}
 finally{destroyString(key);}
 context.constructed=true;
 session.writePointer(callbackVtable);
 const strategy=session.add(8).readPointer();
 context.strategy=strategy;sessions.set(session.toString(),context);
 return {session:session.toString(),strategy:strategy.toString(),strategyVtable:strategy.readPointer().sub(base).toString(),id,scene,codec};
}
async function finalizeRequest(context){
 if(!context)return {cleaned:true};
 // Both recognize() and the Python error path can ask for cleanup. Share one
 // promise so destruction is never retried or applied to another live session.
 if(context.cleanupPromise)return context.cleanupPromise;
 context.closing=true;
 context.cleanupPromise=(async()=>{
  try{
   if(context.session){
    if(!context.constructed)throw Error('Native constructor did not complete; retain agent');
    await drainNativeTasks(context);
    await onCoroutine(()=>{
     if(nativeTaskReferences(context)!==1)throw Error('Native task ownership changed before destruction');
     context.cleanupStarted=true;destroySession(context.session);
    });
    sessions.delete(context.session.toString());
   }
   requests.delete(context.id);context.keep.length=0;return {cleaned:true};
  }catch(e){cleanupFailed=true;send({event:'native_cleanup_failed',id:context.id,error:String(e)});return {cleaned:false,cancelled:true,error:String(e)};}
 })();
 return context.cleanupPromise;
}
rpc.exports={
 info(){return {pid:Process.id,base:base.toString(),path:mod.path,size:mod.size,arch:Process.arch};},
 config(){const ilink=native(0x6903940,'uint8',['int']); const opus=native(0x46c2880,'int',[]);return {voice_input_ilink:ilink(36),voice_input_opus:opus()};},
 ready(){const kernel=native(0x425a0,'pointer',[])();return {kernel:!kernel.isNull(),account:!kernel.isNull()&&kernel.add(0x148).readU32()!==0};},
 read(rva,n){return Array.from(new Uint8Array(base.add(rva).readByteArray(n)));},
 vtable(rva,count){return Array.from({length:count},(_,i)=>base.add(rva+i*8).readPointer().sub(base).toString());},
 encode(pcm){return encode(pcm);},
 state(address){const p=ptr(address);return {final:p.add(0xc7).readU8(),stopping:p.add(0xc5).readU8(),text:textOf(p.add(0xc8)),seq:p.add(0x110).readS32(),extra:textOf(p.add(0xf0))};},
 stats(){return {inFlight,sessions:sessions.size,requests:requests.size,queuedDispatches:pending.length,cleanupFailed,disposing,disposed};},
 async finalize(id){
  if(id!==undefined){
   const context=requests.get(id);
   if(context&&context.running&&!context.closing)return {cleaned:false,error:'Request is still running'};
   return finalizeRequest(context);
  }
  if(inFlight)return {cleaned:false,error:'Other requests are still running'};
  const results=await Promise.all(Array.from(requests.values(),context=>finalizeRequest(context)));
  return {cleaned:results.every(result=>result.cleaned)};
 },
 async dispose(){
  if(disposed)return {cleaned:true};
  disposing=true;
  const result=await rpc.exports.finalize();if(!result.cleaned)return result;
  disposed=true;
  coroutineHook.detach();responseHook.detach();resultHook.detach();errorHook.detach();Interceptor.flush();
  return {cleaned:true,hooksDetached:true,nativeFallbacksRetained:true};
 },
 async recognize(pcm,id,scene=36,codec=5){
  if(disposed||disposing)throw Error('Agent is stopping');
  if(cleanupFailed)throw Error('Native cleanup failed; retain agent until Weixin exits');
  if(requests.has(id)||!/^[\x20-\x7e]+$/.test(id))throw Error('Unique ASCII session id required');
  const context={id,session:null,strategy:null,keep:[],events:[],done:false,closing:false,cleanupStarted:false,cleanupPromise:null,running:true};
  requests.set(id,context);inFlight++;
  try{
   const enc=encode(pcm,context.keep);if(enc.failed||!enc.data.length)throw Error('encoding failed');
   const packets=opusPackets(enc.data.bytes,enc.frames);
   let created;
   send({event:'waiting_for_coroutine',id,timeout_seconds:30});
   await onCoroutine(()=>{
    if(cleanupFailed)throw Error('Native cleanup failed');
    const cfg=rpc.exports.config();
    if(cfg.voice_input_ilink!==1||cfg.voice_input_opus!==1)throw Error('This PoC requires a logged-in client with iLink and Opus enabled');
    created=createSession(context,scene,codec);send({event:'session_created',...created});
    const method=context.strategy.readPointer().add(8).readPointer();
    const submit=new NativeFunction(method,'void',['pointer','pointer','int','uint8','int'],{abi:'win64',exceptions:'steal'});
    for(let i=0;i<packets.length;i++){
     const audio=emptyString(context.keep);assignString(audio,packets[i]);
     try{submit(context.strategy,audio,i+1,i===packets.length-1?1:0,codec);}
     finally{destroyString(audio);}
    }
    send({event:'request_submitted',id,bytes:enc.data.length,frames:enc.frames,packets:packets.length});
   });
   for(let i=0;i<600;i++){
    if(context.session.add(0xc7).readU8()){
     const event={event:'final_result',text:textOf(context.session.add(0xc8)),a:context.session.add(0xe8).readS32(),b:context.session.add(0xec).readS32()};
     context.events.push(event);send({...event,id});context.done=true;
    }
    if(context.done)break;
    await new Promise(resolve=>setTimeout(resolve,100));
   }
   const snapshot={...created,bytes:enc.data.length,frames:enc.frames,done:context.done,events:context.events};
   const cleanup=await finalizeRequest(context);snapshot.cleaned=cleanup.cleaned;return snapshot;
  }finally{
   context.running=false;
   await finalizeRequest(context);
   inFlight--;
  }
 }
};
send({event:'loaded',pid:Process.id});
