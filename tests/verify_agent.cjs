// Exercise the real agent scheduler and cleanup logic without loading Weixin.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

function harness() {
  class Pointer {
    constructor(address) { this.address = address; }
    add(n) { return new Pointer(this.address + n); }
    readPointer() { return new Pointer(999); }
    readByteArray(n) { return new Uint8Array(n); }
    writeByteArray() {}
    writePointer() {}
    isNull() { return false; }
    equals(other) { return this.address === other.address; }
    toString() { return String(this.address); }
  }
  const hooks = [], callbacks = [], timers = new Map(), destroyed = [];
  let address = 10000, timerId = 0;
  const context = vm.createContext({
    Process: { getModuleByName: () => ({base: new Pointer(0)}), getCurrentThreadId: () => 1 },
    NativeFunction: function() { return () => new Pointer(address++); },
    NativeCallback: function(fn) { callbacks.push(fn); return fn; },
    Interceptor: { attach: (_, callbacks) => { hooks.push(callbacks); return {detach() {}}; }, flush() {} },
    send() {}, rpc: {},
    setTimeout: fn => { const id = ++timerId; timers.set(id, fn); return id; },
    clearTimeout: id => timers.delete(id),
    testDestroy: pointer => destroyed.push(pointer.toString()),
  });
  const source = fs.readFileSync(path.join(__dirname, '..', 'agent.js'), 'utf8');
  vm.runInContext(source + '\n destroySession=testDestroy; globalThis.test={onCoroutine,finalizeRequest,acceptsCallback,pending,requests,sessions,get cleanupFailed(){return cleanupFailed;}};', context);
  return { test: context.test, exports: context.rpc.exports, hooks, callbacks, timers, destroyed, Pointer, context,
    tick: () => hooks[0].onLeave(new Pointer(1)) };
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

  // A dispatch timeout removes only its own job. The other job still runs.
  const timedOut = h.test.onCoroutine(() => order.push('must-not-run'));
  const rejected = assert.rejects(timedOut, /No coroutine dispatch/);
  const survivor = h.test.onCoroutine(() => order.push('survivor'));
  h.timers.values().next().value();
  h.tick();
  await rejected; await survivor;
  assert.equal(order.at(-1), 'survivor');
  assert.equal(h.test.pending.length, 0);

  function request(id, address) {
    const context = {id, session: new h.Pointer(address), strategy: new h.Pointer(address + 1),
      constructed: true, closing: false, cleanupStarted: false, cleanupPromise: null, running: false, keep: [{}], events: [], done: false};
    h.test.requests.set(id, context);
    h.test.sessions.set(context.session.toString(), context);
    return context;
  }
  const a = request('A', 200), b = request('B', 300);
  assert.equal(h.test.acceptsCallback(a.session), a);
  assert.equal(h.test.acceptsCallback(b.session), b);
  h.callbacks[1](a.session, 1, 42, 0);
  assert.equal(a.done, true);
  assert.equal(a.events[0].code, 42);
  assert.equal(b.done, false);
  assert.equal(b.events.length, 0);
  const cleanupA = h.test.finalizeRequest(a);
  const repeatedCleanupA = h.test.finalizeRequest(a);
  assert.equal(h.test.acceptsCallback(a.session), null);
  assert.equal(h.test.acceptsCallback(b.session), b);
  assert.equal(h.test.pending.length, 1);
  h.callbacks[1](a.session, 1, 99, 0);
  assert.equal(a.events.length, 1);
  h.tick();
  assert.equal((await cleanupA).cleaned, true);
  assert.equal((await repeatedCleanupA).cleaned, true);
  assert.deepEqual(h.destroyed, ['200']);
  assert.equal(h.test.requests.has('B'), true);
  assert.equal(a.keep.length, 0);
  assert.equal(b.keep.length, 1);

  // Native destruction failure retains callbacks and allocations, blocks new
  // work, and cannot cause a second destructor call or script disposal.
  vm.runInContext('destroySession=()=>{throw Error("injected destruction failure");};', h.context);
  const cleanupB = h.test.finalizeRequest(b);
  h.tick();
  assert.equal((await cleanupB).cleaned, false);
  assert.equal(h.test.cleanupFailed, true);
  assert.equal(h.test.sessions.has('300'), true);
  assert.equal(b.keep.length, 1);
  assert.equal((await h.test.finalizeRequest(b)).cleaned, false);
  assert.equal(h.test.pending.length, 0);
  assert.equal((await h.exports.dispose()).cleaned, false);
  assert.equal(h.exports.stats().disposed, false);
  console.log('Agent dispatch isolation, late-callback filtering and fail-closed cleanup passed.');
}
main().catch(error => { console.error(error); process.exitCode = 1; });
