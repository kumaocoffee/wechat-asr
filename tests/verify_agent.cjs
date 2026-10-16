// Exercise the real agent scheduler and cleanup logic without loading Weixin.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
  const pointers = new Map(), references = new Map(), nativeCalls = [], detached = [];
  class Pointer {
    constructor(address) { this.address = address; }
    add(n) { return new Pointer(this.address + n); }
    readPointer() { return new Pointer(pointers.get(this.address) ?? 999); }
    readS32() { return references.get(this.address) ?? 1; }
    readByteArray(n) { return new Uint8Array(n); }
    writeByteArray() {}
    writePointer() {}
    isNull() { return false; }
    equals(other) { return this.address === other.address; }
    toString() { return String(this.address); }
    toInt32() { return this.address; }
  }
  const hooks = [], callbacks = [], timers = new Map(), destroyed = [];
  let address = 10000, timerId = 0, now=0;
  const context = vm.createContext({
    Process: { getModuleByName: () => ({base: new Pointer(0),getExportByName:()=>new Pointer(9999)}), getCurrentThreadId: () => 1 },
    NativeFunction: function(target) { return (...args) => {nativeCalls.push({target:target.address,args});return new Pointer(address++);}; },
    NativeCallback: function(fn) { callbacks.push(fn); return fn; },
    Interceptor: { attach: (_, callbacks) => { const id=hooks.length;hooks.push(callbacks); return {detach() {detached.push(id);}}; }, flush() {} },
    Memory: {protect:()=>true}, ptr: n=>new Pointer(n),
    Date: class extends Date {static now(){return now;}},
    send() {}, rpc: {},
    setTimeout: fn => { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
    testDestroy: pointer => destroyed.push(pointer.toString()),
  });
  const source = fs.readFileSync(path.join(__dirname, '..', 'agent.js'), 'utf8');
  vm.runInContext(source + '\n destroySession=testDestroy; globalThis.test={onCoroutine,finalizeRequest,acceptsCallback,pending,requests,sessions,get cleanupFailed(){return cleanupFailed;}};', context);
  return { test: context.test, exports: context.rpc.exports, hooks, callbacks, timers, destroyed, Pointer, context,pointers,references,nativeCalls,detached,
    tick: () => hooks[2].onLeave(new Pointer(1)),
    error: (self,flag,code,detail)=>hooks[1].onEnter([self,new Pointer(flag),new Pointer(code),new Pointer(detail)]),
    advance: ms=>{now+=ms;},
    flush: async()=>{for(let i=0;i<8;i++)await Promise.resolve();} };
}

async function main() {
  const h = harness(), order = [];
  const first = h.test.onCoroutine(() => order.push('first'));
  const second = h.test.onCoroutine(() => order.push('second'));
  assert.equal(h.test.pending.length, 2);
  h.tick(); h.tick();
  await Promise.all([first, second]);
  assert.deepEqual(order, ['first', 'second']);
  assert.equal(h.timers.size, 0);

  // Native calls re-enter the hooked coroutine function. A cleanup job must
  // not run inside an earlier native action that is still using its objects.
  const nestedOrder=[];
  const outer=h.test.onCoroutine(()=>{nestedOrder.push('outer-start');h.tick();nestedOrder.push('outer-end');});
  const nested=h.test.onCoroutine(()=>nestedOrder.push('next'));
  h.tick();
  assert.deepEqual(nestedOrder,['outer-start','outer-end']);
  h.tick();await outer;await nested;
  assert.deepEqual(nestedOrder,['outer-start','outer-end','next']);

  // A dispatch timeout removes only its own job. The other job still runs.
  const timedOut = h.test.onCoroutine(() => order.push('must-not-run'));
  const rejected = assert.rejects(timedOut, /No coroutine dispatch/);
  const survivor = h.test.onCoroutine(() => order.push('survivor'));
  const timeoutTimer=h.timers.entries().next().value;
  h.timers.delete(timeoutTimer[0]);timeoutTimer[1]();
  h.tick();
  await rejected; await survivor;
  assert.equal(order.at(-1), 'survivor');
  assert.equal(h.test.pending.length, 0);

  function request(id, address, target=h) {
    const context = {id, session: new target.Pointer(address), strategy: new target.Pointer(address + 1),
      constructed: true, closing: false, cleanupStarted: false, cleanupPromise: null, running: false, keep: [{}], events: [], done: false};
    target.test.requests.set(id, context);
    target.test.sessions.set(context.session.toString(), context);
    target.pointers.set(address+1,0x991de58);
    target.pointers.set(address+1+0x58,address+2000);
    target.references.set(address+2000+8,1);
    return context;
  }
  const a = request('A', 200), b = request('B', 300);
  assert.equal(h.test.acceptsCallback(a.session), a);
  assert.equal(h.test.acceptsCallback(b.session), b);
  h.error(a.session, 1, 42, 0);
  assert.equal(a.done, true);
  assert.equal(a.events[0].code, 42);
  assert.equal(b.done, false);
  assert.equal(b.events.length, 0);
  const cleanupA = h.test.finalizeRequest(a);
  const repeatedCleanupA = h.test.finalizeRequest(a);
  assert.equal(h.test.acceptsCallback(a.session), null);
  assert.equal(h.test.acceptsCallback(b.session), b);
  assert.equal(h.test.pending.length, 1);
  h.error(a.session, 1, 99, 0);
  assert.equal(a.events.length, 1);
  h.tick();
  await h.flush();h.tick();
  assert.equal((await cleanupA).cleaned, true);
  assert.equal((await repeatedCleanupA).cleaned, true);
  assert.deepEqual(h.destroyed, ['200']);
  assert.equal(h.test.requests.has('B'), true);
  assert.equal(a.keep.length, 0);
  assert.equal(b.keep.length, 1);

  // A completed response does not allow destruction while a native response
  // closure still owns the cancellation shared_ptr. Release ownership later.
  const c=request('C',400);
  h.references.set(2408,2);
  const cleanupC=h.test.finalizeRequest(c);
  h.tick();await h.flush();
  assert.deepEqual(h.destroyed,['200']);
  assert.equal(h.test.pending.length,0);
  h.references.set(2408,1);
  const drainTimer=h.timers.entries().next().value;
  h.timers.delete(drainTimer[0]);drainTimer[1]();
  await h.flush();h.tick();
  assert.equal((await cleanupC).cleaned,true);
  assert.deepEqual(h.destroyed,['200','400']);
  assert.equal(h.callbacks.length,0,'Native object vtables must not point into JS callbacks');

  // Check ownership again when the queued destruction actually executes.
  const d=request('D',500);
  const cleanupD=h.test.finalizeRequest(d);
  h.tick();await h.flush();
  h.references.set(2508,2);h.tick();
  assert.equal((await cleanupD).cleaned,false);
  assert.deepEqual(h.destroyed,['200','400']);
  assert.equal(d.keep.length,1);

  // Native destruction failure retains callbacks and allocations, blocks new
  // work, and cannot cause a second destructor call or script disposal.
  vm.runInContext('destroySession=()=>{throw Error("injected destruction failure");};', h.context);
  const cleanupB = h.test.finalizeRequest(b);
  h.tick();
  await h.flush();h.tick();
  assert.equal((await cleanupB).cleaned, false);
  assert.equal(h.test.cleanupFailed, true);
  assert.equal(h.test.sessions.has('300'), true);
  assert.equal(b.keep.length, 1);
  assert.equal((await h.test.finalizeRequest(b)).cleaned, false);
  assert.equal(h.test.pending.length, 0);
  assert.equal((await h.exports.dispose()).cleaned, false);
  assert.equal(h.exports.stats().disposed, false);
  const clean=harness();
  assert.equal((await clean.exports.dispose()).nativeFallbacksRetained,true);
  assert.equal(clean.detached.length,4);
  assert.equal(clean.nativeCalls.filter(call=>call.target===0x7165a28).length,0,
    'Process-owned callback fallbacks must survive script unload');

  const stuck=harness(), retained=request('retained',600,stuck);
  stuck.references.set(2608,2);
  const timeoutCleanup=stuck.test.finalizeRequest(retained);
  stuck.tick();await stuck.flush();stuck.advance(30001);
  const ownershipTimer=stuck.timers.entries().next().value;
  stuck.timers.delete(ownershipTimer[0]);ownershipTimer[1]();
  await stuck.flush();
  assert.equal((await timeoutCleanup).cleaned,false);
  assert.equal(stuck.destroyed.length,0);
  assert.equal(retained.keep.length,1);
  assert.equal((await stuck.exports.dispose()).cleaned,false);
  assert.equal(stuck.detached.length,0);
  console.log('Native ownership drain, reentrant dispatch isolation, disconnect-safe callbacks and fail-closed cleanup passed.');
}
const deadline=setTimeout(()=>{console.error('Agent verification did not finish');process.exit(1);},5000);
main().then(()=>clearTimeout(deadline)).catch(error => { clearTimeout(deadline);console.error(error); process.exitCode = 1; });
