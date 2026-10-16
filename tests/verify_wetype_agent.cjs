'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
let address = 1, tick = 0, timerId = 0, nativeId = 0;
const timers = new Map(), submissions = [];
class Pointer {
 constructor(size=16) { this.bytes=new Uint8Array(size); this.address=address++; }
 toString() { return String(this.address); }
 writeByteArray(bytes) { this.bytes.set(bytes); return this; }
 writeU32(value) { new DataView(this.bytes.buffer).setUint32(0,value,true); }
 add(offset) { return {writeU8:value=>{this.bytes[offset]=value;}}; }
 readByteArray(length) { return this.bytes.slice(0,length).buffer; }
 isNull() { return false; }
}
const context = {
 Process:{getModuleByName:name=>({base:{add:rva=>rva},getExportByName:name=>name})},
 Memory:{alloc:size=>new Pointer(size)}, rpc:{exports:{}}, Uint8Array,
 NativeCallback:function(callback){return callback;},
 NativeFunction:function(name){
  if(name==='business_net_start_task_with_option') return (...args)=>{
   const id=++nativeId; submissions.push({args,id}); return id;
  };
  return ()=>0;
 },
 setTimeout(callback,delay){const id=++timerId;timers.set(id,{callback,at:tick+delay});return id;},
 clearTimeout(id){timers.delete(id);}
};
function advance(ms) {
 tick+=ms;
 for(const [id,timer] of [...timers])if(timer.at<=tick){timers.delete(id);timer.callback();}
}
function respond(submission) {
 const {args,id}=submission;
 const payload=new Pointer(1);payload.writeByteArray([7]);
 args[5](args[4],id,0,payload,1);
}
vm.runInNewContext(fs.readFileSync(path.join(__dirname,'..','wetype_agent.js'),'utf8'),context);
const agent=context.rpc.exports;
(async()=>{
 const a=agent.request([1]),b=agent.request([2]);
 assert.equal(agent.finalize().cleaned,false);
 respond(submissions[0]);
 assert.equal(agent.finalize().cleaned,false,'native callback has not returned');
 advance(250);await a;
 assert.equal(agent.stats().pending,1,'the second request must retain its own buffers');
 respond(submissions[1]);advance(250);await b;
 assert.equal(agent.finalize().cleaned,true);
 const delayed=agent.request([3]);
 const caught=assert.rejects(delayed,/timed out/);
 advance(15000);await caught;
 assert.equal(agent.stats().pending,1,'timeout cannot free live native callback data');
 assert.equal(agent.dispose().cleaned,false,'shutdown must retain the script while native owns callbacks');
 respond(submissions[2]);advance(250);await Promise.resolve();
 assert.equal(agent.dispose().cleaned,true);
 assert.throws(()=>agent.request([4]),/stopping/);
 console.log('WeType native callback ownership checks passed');
})().catch(error=>{console.error(error);process.exitCode=1;});
