"""Lua scripts. Each one is one atomic write, so the indexes cannot go out of step.

The scripts build key names from the prefix in ARGV[1]. Thus the store must be one
node (Render Key Value is). Scores stay strings in Lua, because tostring() of a
float loses digits.
"""

_COMMON = """
local p = ARGV[1]
local STATUSES = {'todo', 'in_progress', 'done', 'cancelled'}
local function is_open(s) return s == 'todo' or s == 'in_progress' end
local QUESTION_STATUSES = {'pending', 'answered', 'expired', 'cancelled'}
-- Deletes the inbox of one key.
local function drop_inbox(keyk)
  local base = keyk .. ':inbox'
  local ids = redis.call('ZRANGE', base, 0, -1)
  for i = 1, #ids, 500 do
    local docs = {}
    for j = i, math.min(i + 499, #ids) do docs[#docs + 1] = p .. ':inb:' .. ids[j] end
    redis.call('DEL', unpack(docs))
  end
  redis.call('DEL', base, base .. ':unread', base .. ':read')
end
-- Deletes the questions of one key. `acck` is the account key, or false if the account indexes go away with it.
local function drop_questions(keyk, acck)
  for _, s in ipairs(QUESTION_STATUSES) do
    local idx = keyk .. ':qst:' .. s
    local ids = redis.call('ZRANGE', idx, 0, -1)
    for i = 1, #ids, 500 do
      local chunk, docs = {}, {}
      for j = i, math.min(i + 499, #ids) do
        chunk[#chunk + 1] = ids[j]
        docs[#docs + 1] = p .. ':qst:' .. ids[j]
      end
      redis.call('DEL', unpack(docs))
      redis.call('ZREM', p .. ':qexp', unpack(chunk))
      redis.call('ZREM', p .. ':qclosed', unpack(chunk))
      if acck then redis.call('ZREM', acck .. ':qst:' .. s, unpack(chunk)) end
    end
    redis.call('DEL', idx)
  end
end
"""

AUTH = """
-- ARGV: prefix, cred hash, now, rate limit, rate bucket, rate ttl, touch interval, inbox unread cutoff
-- Returns {0} for an unknown credential, {2, cred} above the rate limit, else {1, cred, unread inbox items}.
local p = ARGV[1]
local cred = redis.call('GET', p .. ':cred:' .. ARGV[2])
if not cred then return {0} end
local limit = tonumber(ARGV[4])
if limit > 0 then
  local rk = p .. ':rl:' .. ARGV[5]
  local n = redis.call('INCR', rk)
  if n == 1 then redis.call('EXPIRE', rk, ARGV[6]) end
  if n > limit then return {2, cred} end
end
local key_id = string.match(cred, '^k:[^:]+:(.+)$')
if key_id and redis.call('SET', p .. ':touch:' .. key_id, '1', 'NX', 'EX', ARGV[7]) then
  redis.call('HSET', p .. ':key:' .. key_id, 'last_used_at', ARGV[3])
end
local unread = 0
if key_id then
  unread = redis.call('ZCOUNT', p .. ':key:' .. key_id .. ':inbox:unread', '(' .. ARGV[8], '+inf')
end
return {1, cred, unread}
"""

RATE_HIT = """
-- KEYS[1] counter; ARGV: ttl
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""

HASH_UPDATE = """
-- KEYS[1] hash; ARGV: guard field, guard value, then field/value pairs
-- Returns false if the guard fails, else {1 if a value changed else 0, the hash}.
if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return false end
local changed = 0
for i = 3, #ARGV, 2 do
  if redis.call('HGET', KEYS[1], ARGV[i]) ~= ARGV[i + 1] then
    redis.call('HSET', KEYS[1], ARGV[i], ARGV[i + 1])
    changed = 1
  end
end
return {changed, redis.call('HGETALL', KEYS[1])}
"""

HASH_CLEAR = """
-- KEYS[1] hash, KEYS[2] optional key to delete with it; ARGV: field, expected value, 'del' or 'empty'
if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return 0 end
if ARGV[3] == 'del' then
  redis.call('HDEL', KEYS[1], ARGV[1])
else
  redis.call('HSET', KEYS[1], ARGV[1], '')
end
if KEYS[2] then redis.call('DEL', KEYS[2]) end
return 1
"""

DEVICE_PUT = """
-- ARGV: prefix, account, device, max devices, then field/value pairs
local p, aid, did = ARGV[1], ARGV[2], ARGV[3]
if redis.call('EXISTS', p .. ':acc:' .. aid) == 0 then return 'no_account' end
local setk = p .. ':acc:' .. aid .. ':devices'
local devk = p .. ':dev:' .. aid .. ':' .. did
if redis.call('SISMEMBER', setk, did) == 0 and redis.call('SCARD', setk) >= tonumber(ARGV[4]) then
  return 'limit'
end
local fields, token = {}, ''
for i = 5, #ARGV, 2 do
  fields[#fields + 1] = ARGV[i]
  fields[#fields + 1] = ARGV[i + 1]
  if ARGV[i] == 'apns_token' then token = ARGV[i + 1] end
end
local owner = aid .. ':' .. did
local old = redis.call('HGET', devk, 'apns_token')
if old and old ~= '' and old ~= token and redis.call('GET', p .. ':tok:' .. old) == owner then
  redis.call('DEL', p .. ':tok:' .. old)
end
if token ~= '' then
  -- One APNs token is one app install. A second registration of it replaces the first.
  local holder = redis.call('GET', p .. ':tok:' .. token)
  if holder and holder ~= owner then
    local oa, od = string.match(holder, '^([^:]+):(.+)$')
    local other = p .. ':dev:' .. oa .. ':' .. od
    if redis.call('EXISTS', other) == 1 then
      redis.call('HSET', other, 'apns_token', '', 'push_to_start_token', '', 'widget_push_token', '')
    end
  end
  redis.call('SET', p .. ':tok:' .. token, owner)
end
redis.call('DEL', devk)
redis.call('HSET', devk, unpack(fields))
redis.call('SADD', setk, did)
return 'ok'
"""

DEVICE_DELETE = """
-- ARGV: prefix, account, device
local p, aid, did = ARGV[1], ARGV[2], ARGV[3]
if redis.call('SREM', p .. ':acc:' .. aid .. ':devices', did) == 0 then return 0 end
local devk = p .. ':dev:' .. aid .. ':' .. did
local token = redis.call('HGET', devk, 'apns_token')
if token and token ~= '' and redis.call('GET', p .. ':tok:' .. token) == aid .. ':' .. did then
  redis.call('DEL', p .. ':tok:' .. token)
end
redis.call('DEL', devk, p .. ':wdg:' .. aid .. ':' .. did, p .. ':wdgd:' .. aid .. ':' .. did)
return 1
"""

KEY_CREATE = """
-- ARGV: prefix, account, key, cred hash, max keys, created_at, label, secret prefix, last4, event
local p, aid, kid = ARGV[1], ARGV[2], ARGV[3]
if redis.call('EXISTS', p .. ':acc:' .. aid) == 0 then return 'no_account' end
local idx = p .. ':acc:' .. aid .. ':keys'
if redis.call('ZCARD', idx) >= tonumber(ARGV[5]) then return 'limit' end
redis.call('HSET', p .. ':key:' .. kid, 'account_id', aid, 'cred', ARGV[4], 'created_at', ARGV[6],
  'label', ARGV[7], 'prefix', ARGV[8], 'last4', ARGV[9])
redis.call('ZADD', idx, ARGV[6], kid)
redis.call('SET', p .. ':cred:' .. ARGV[4], 'k:' .. aid .. ':' .. kid)
redis.call('PUBLISH', p .. ':ev:' .. aid, ARGV[10])
return 'ok'
"""

KEY_DELETE = (
    _COMMON
    + """
-- ARGV: prefix, account, key, event
-- Returns false if the key is not there, else {task json, activity pairs} for each task with a Live Activity.
local aid, kid = ARGV[2], ARGV[3]
local keyk = p .. ':key:' .. kid
local h = redis.call('HMGET', keyk, 'account_id', 'cred')
if h[1] ~= aid then return false end
local acc_tasks = p .. ':acc:' .. aid .. ':tasks'
local key_tasks = keyk .. ':tasks'
local ids = redis.call('ZRANGE', key_tasks, 0, -1)
local ended = {}
for i = 1, #ids, 500 do
  local chunk, tkeys = {}, {}
  for j = i, math.min(i + 499, #ids) do
    local id = ids[j]
    local lak = p .. ':la:' .. id
    local la = redis.call('HGETALL', lak)
    if #la > 0 then
      ended[#ended + 1] = {redis.call('GET', p .. ':tsk:' .. id), la}
      redis.call('DEL', lak)
    end
    chunk[#chunk + 1] = id
    tkeys[#tkeys + 1] = p .. ':tsk:' .. id
    tkeys[#tkeys + 1] = p .. ':tq:' .. id
  end
  redis.call('DEL', unpack(tkeys))
  redis.call('ZREM', p .. ':closed', unpack(chunk))
  redis.call('ZREM', acc_tasks, unpack(chunk))
  for _, s in ipairs(STATUSES) do redis.call('ZREM', acc_tasks .. ':' .. s, unpack(chunk)) end
end
drop_questions(keyk, p .. ':acc:' .. aid)
drop_inbox(keyk)
redis.call('DEL', keyk, key_tasks, p .. ':touch:' .. kid, p .. ':cred:' .. h[2])
for _, s in ipairs(STATUSES) do redis.call('DEL', key_tasks .. ':' .. s) end
redis.call('ZREM', p .. ':acc:' .. aid .. ':keys', kid)
redis.call('PUBLISH', p .. ':ev:' .. aid, ARGV[4])
return ended
"""
)

ACCOUNT_DELETE = (
    _COMMON
    + """
-- ARGV: prefix, account, event
local aid = ARGV[2]
local acck = p .. ':acc:' .. aid
local cred = redis.call('HGET', acck, 'cred')
if not cred then return 0 end
local acc_tasks = acck .. ':tasks'
local ids = redis.call('ZRANGE', acc_tasks, 0, -1)
for i = 1, #ids, 500 do
  local chunk, keys = {}, {}
  for j = i, math.min(i + 499, #ids) do
    chunk[#chunk + 1] = ids[j]
    keys[#keys + 1] = p .. ':tsk:' .. ids[j]
    keys[#keys + 1] = p .. ':la:' .. ids[j]
    keys[#keys + 1] = p .. ':tq:' .. ids[j]
  end
  redis.call('DEL', unpack(keys))
  redis.call('ZREM', p .. ':closed', unpack(chunk))
end
for _, kid in ipairs(redis.call('ZRANGE', acck .. ':keys', 0, -1)) do
  local keyk = p .. ':key:' .. kid
  local kc = redis.call('HGET', keyk, 'cred')
  if kc then redis.call('DEL', p .. ':cred:' .. kc) end
  drop_questions(keyk, false)
  drop_inbox(keyk)
  redis.call('DEL', keyk, keyk .. ':tasks', p .. ':touch:' .. kid)
  for _, s in ipairs(STATUSES) do redis.call('DEL', keyk .. ':tasks:' .. s) end
end
for _, did in ipairs(redis.call('SMEMBERS', acck .. ':devices')) do
  local devk = p .. ':dev:' .. aid .. ':' .. did
  local token = redis.call('HGET', devk, 'apns_token')
  if token and token ~= '' and redis.call('GET', p .. ':tok:' .. token) == aid .. ':' .. did then
    redis.call('DEL', p .. ':tok:' .. token)
  end
  redis.call('DEL', devk, p .. ':wdg:' .. aid .. ':' .. did, p .. ':wdgd:' .. aid .. ':' .. did)
end
local attested = redis.call('HGET', acck, 'attest_key_id')
if attested then redis.call('DEL', p .. ':attk:' .. attested) end
redis.call('DEL', acck, acck .. ':keys', acck .. ':devices', acc_tasks, p .. ':cred:' .. cred)
for _, s in ipairs(STATUSES) do redis.call('DEL', acc_tasks .. ':' .. s) end
for _, s in ipairs(QUESTION_STATUSES) do redis.call('DEL', acck .. ':qst:' .. s) end
redis.call('PUBLISH', p .. ':ev:' .. aid, ARGV[3])
return 1
"""
)

_INDEX = """
local function index(aid, kid, tid, status, score)
  local kt, at = p .. ':key:' .. kid .. ':tasks', p .. ':acc:' .. aid .. ':tasks'
  redis.call('ZADD', kt, score, tid)
  redis.call('ZADD', at, score, tid)
  redis.call('ZADD', kt .. ':' .. status, score, tid)
  redis.call('ZADD', at .. ':' .. status, score, tid)
  if is_open(status) then
    redis.call('ZREM', p .. ':closed', tid)
  else
    redis.call('ZADD', p .. ':closed', score, tid)
  end
end
local function open_count(keyk)
  return tonumber(redis.call('HGET', keyk, 'n:todo') or '0')
    + tonumber(redis.call('HGET', keyk, 'n:in_progress') or '0')
end
"""

TASK_CREATE = (
    _COMMON
    + _INDEX
    + """
-- ARGV: prefix, account, key, task, json, status, score, open limit, event
local aid, kid, tid, status = ARGV[2], ARGV[3], ARGV[4], ARGV[6]
local keyk = p .. ':key:' .. kid
local tk = p .. ':tsk:' .. tid
if redis.call('HGET', keyk, 'account_id') ~= aid then return 'no_key' end
-- The client sends a write again after a lost connection. The task is then there already.
if redis.call('EXISTS', tk) == 1 then return 'ok' end
if is_open(status) and open_count(keyk) >= tonumber(ARGV[8]) then return 'limit' end
redis.call('SET', tk, ARGV[5])
index(aid, kid, tid, status, ARGV[7])
redis.call('HINCRBY', keyk, 'n:' .. status, 1)
redis.call('PUBLISH', p .. ':ev:' .. aid, ARGV[9])
return 'ok'
"""
)

TASK_UPDATE = (
    _COMMON
    + _INDEX
    + """
-- ARGV: prefix, account, key, task, old json, new json, old status, new status, score, open limit, event
-- The old json is a compare-and-swap guard: a concurrent write gives 'conflict'.
local aid, kid, tid, old, new = ARGV[2], ARGV[3], ARGV[4], ARGV[7], ARGV[8]
local tk = p .. ':tsk:' .. tid
if redis.call('GET', tk) ~= ARGV[5] then return 'conflict' end
local keyk = p .. ':key:' .. kid
if old ~= new then
  if is_open(new) and not is_open(old) and open_count(keyk) >= tonumber(ARGV[10]) then return 'limit' end
  redis.call('ZREM', keyk .. ':tasks:' .. old, tid)
  redis.call('ZREM', p .. ':acc:' .. aid .. ':tasks:' .. old, tid)
  redis.call('HINCRBY', keyk, 'n:' .. old, -1)
  redis.call('HINCRBY', keyk, 'n:' .. new, 1)
end
redis.call('SET', tk, ARGV[6])
index(aid, kid, tid, new, ARGV[9])
redis.call('PUBLISH', p .. ':ev:' .. aid, ARGV[11])
return 'ok'
"""
)

TASK_DELETE = """
-- ARGV: prefix, account, key, task, json, status, event
-- Returns false on a concurrent write, else {the Live Activity pairs, the pending question IDs} of the task.
local p, aid, kid, tid, status = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[6]
local tk = p .. ':tsk:' .. tid
if redis.call('GET', tk) ~= ARGV[5] then return false end
local kt, at = p .. ':key:' .. kid .. ':tasks', p .. ':acc:' .. aid .. ':tasks'
redis.call('DEL', tk)
redis.call('ZREM', kt, tid)
redis.call('ZREM', at, tid)
redis.call('ZREM', kt .. ':' .. status, tid)
redis.call('ZREM', at .. ':' .. status, tid)
redis.call('ZREM', p .. ':closed', tid)
redis.call('HINCRBY', p .. ':key:' .. kid, 'n:' .. status, -1)
local lak, tqk = p .. ':la:' .. tid, p .. ':tq:' .. tid
local la = redis.call('HGETALL', lak)
local questions = redis.call('ZRANGE', tqk, 0, -1)
redis.call('DEL', lak, tqk)
redis.call('PUBLISH', p .. ':ev:' .. aid, ARGV[7])
return {la, questions}
"""

TASK_PAGE = """
-- ARGV: index zset, task key prefix, count, cursor score ('' = from the top), cursor id, statuses...
-- Returns {has_more, last score, last id, json...}, newest first. With statuses, it merges the status indexes.
local base, tprefix, count, cscore, cid = ARGV[1], ARGV[2], tonumber(ARGV[3]), ARGV[4], ARGV[5]

-- Byte order, the order that a sorted set uses for members with equal scores.
local function lt(a, b)
  local n = math.min(#a, #b)
  for i = 1, n do
    local x, y = string.byte(a, i), string.byte(b, i)
    if x ~= y then return x < y end
  end
  return #a < #b
end

local function after_cursor(zkey)
  if cscore == '' then
    return redis.call('ZREVRANGEBYSCORE', zkey, '+inf', '-inf', 'WITHSCORES', 'LIMIT', 0, count + 1)
  end
  local out = {}
  local ties = redis.call('ZRANGEBYSCORE', zkey, cscore, cscore)
  for i = #ties, 1, -1 do
    if lt(ties[i], cid) then
      out[#out + 1] = ties[i]
      out[#out + 1] = cscore
    end
  end
  local rest = redis.call('ZREVRANGEBYSCORE', zkey, '(' .. cscore, '-inf', 'WITHSCORES', 'LIMIT', 0, count + 1)
  for i = 1, #rest do out[#out + 1] = rest[i] end
  return out
end

local lists, pos = {}, {}
if #ARGV < 6 then
  lists[1] = after_cursor(base)
else
  for i = 6, #ARGV do lists[#lists + 1] = after_cursor(base .. ':' .. ARGV[i]) end
end
for i = 1, #lists do pos[i] = 1 end

local ids, scores = {}, {}
while #ids <= count do
  local best = nil
  for i = 1, #lists do
    local k = pos[i]
    if k <= #lists[i] then
      if not best then
        best = i
      else
        local bk = pos[best]
        local s, bs = tonumber(lists[i][k + 1]), tonumber(lists[best][bk + 1])
        if s > bs or (s == bs and lt(lists[best][bk], lists[i][k])) then best = i end
      end
    end
  end
  if not best then break end
  local k = pos[best]
  ids[#ids + 1] = lists[best][k]
  scores[#scores + 1] = lists[best][k + 1]
  pos[best] = k + 2
end

local n = math.min(#ids, count)
if n == 0 then return {0, '', ''} end
local keys = {}
for i = 1, n do keys[i] = tprefix .. ids[i] end
local out = {(#ids > count) and 1 or 0, scores[n], ids[n]}
local docs = redis.call('MGET', unpack(keys))
for i = 1, n do
  if docs[i] then out[#out + 1] = docs[i] end
end
return out
"""

ACTIVITY_PUT = """
-- ARGV: prefix, account, task, device, token
local p, aid = ARGV[1], ARGV[2]
if not redis.call('ZSCORE', p .. ':acc:' .. aid .. ':tasks', ARGV[3]) then return 'no_task' end
if redis.call('SISMEMBER', p .. ':acc:' .. aid .. ':devices', ARGV[4]) == 0 then return 'no_device' end
redis.call('HSET', p .. ':la:' .. ARGV[3], ARGV[4], ARGV[5])
return 'ok'
"""

ACTIVITY_DELETE = """
-- ARGV: prefix, account, task, device
local p = ARGV[1]
if not redis.call('ZSCORE', p .. ':acc:' .. ARGV[2] .. ':tasks', ARGV[3]) then return 0 end
redis.call('HDEL', p .. ':la:' .. ARGV[3], ARGV[4])
return 1
"""

ACCOUNT_CREATE = """
-- ARGV: prefix, account, cred hash, created_at, live_activities, attested key id ('' = none), public key, counter
-- One attested key makes one account.
local p, aid, kid = ARGV[1], ARGV[2], ARGV[6]
local fields = {'id', aid, 'created_at', ARGV[4], 'live_activities', ARGV[5], 'cred', ARGV[3]}
if kid ~= '' then
  if not redis.call('SET', p .. ':attk:' .. kid, aid, 'NX') then return 'key_used' end
  for _, v in ipairs({'attest_key_id', kid, 'attest_public_key', ARGV[7], 'attest_counter', ARGV[8]}) do
    fields[#fields + 1] = v
  end
end
redis.call('HSET', p .. ':acc:' .. aid, unpack(fields))
redis.call('SET', p .. ':cred:' .. ARGV[3], 'a:' .. aid)
return 'ok'
"""

QUESTION_CREATE = """
-- ARGV: prefix, account, key, question, json, created_at, expires_at, pending limit, task ('' = none), event
local p, aid, kid, qid, tid = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[9]
local keyk = p .. ':key:' .. kid
local qk = p .. ':qst:' .. qid
if redis.call('HGET', keyk, 'account_id') ~= aid then return 'no_key' end
-- The client sends a write again after a lost connection. The question is then there already.
if redis.call('EXISTS', qk) == 1 then return 'ok' end
if tid ~= '' and not redis.call('ZSCORE', keyk .. ':tasks', tid) then return 'no_task' end
if redis.call('ZCARD', keyk .. ':qst:pending') >= tonumber(ARGV[8]) then return 'limit' end
redis.call('SET', qk, ARGV[5])
redis.call('ZADD', keyk .. ':qst:pending', ARGV[6], qid)
redis.call('ZADD', p .. ':acc:' .. aid .. ':qst:pending', ARGV[6], qid)
redis.call('ZADD', p .. ':qexp', ARGV[7], qid)
if tid ~= '' then redis.call('ZADD', p .. ':tq:' .. tid, ARGV[6], qid) end
redis.call('PUBLISH', p .. ':ev:' .. aid, ARGV[10])
return 'ok'
"""

QUESTION_CLOSE = """
-- ARGV: prefix, account, key, question, old json, new json, new status, created_at, closed at, task ('' = none), event
-- A question leaves `pending` one time. The old json is the guard: the second writer gets 'conflict'.
local p, aid, kid, qid, status, tid = ARGV[1], ARGV[2], ARGV[3], ARGV[4], ARGV[7], ARGV[10]
local qk = p .. ':qst:' .. qid
if redis.call('GET', qk) ~= ARGV[5] then return 'conflict' end
local kq, aq = p .. ':key:' .. kid .. ':qst:', p .. ':acc:' .. aid .. ':qst:'
redis.call('SET', qk, ARGV[6])
redis.call('ZREM', kq .. 'pending', qid)
redis.call('ZREM', aq .. 'pending', qid)
redis.call('ZADD', kq .. status, ARGV[8], qid)
redis.call('ZADD', aq .. status, ARGV[8], qid)
redis.call('ZREM', p .. ':qexp', qid)
redis.call('ZADD', p .. ':qclosed', ARGV[9], qid)
if tid ~= '' then redis.call('ZREM', p .. ':tq:' .. tid, qid) end
redis.call('PUBLISH', p .. ':ev:' .. aid, ARGV[11])
return 'ok'
"""

QUESTION_PURGE = """
-- ARGV: prefix, account, key, question, status
local p, qid = ARGV[1], ARGV[4]
redis.call('DEL', p .. ':qst:' .. qid)
redis.call('ZREM', p .. ':key:' .. ARGV[3] .. ':qst:' .. ARGV[5], qid)
redis.call('ZREM', p .. ':acc:' .. ARGV[2] .. ':qst:' .. ARGV[5], qid)
redis.call('ZREM', p .. ':qclosed', qid)
redis.call('ZREM', p .. ':qexp', qid)
return 1
"""

QUESTION_LIST = """
-- ARGV: index prefix (ends with ':qst:'), question key prefix, count, statuses...
-- Returns the json of the newest questions with one of the statuses, newest first.
local base, qprefix, count = ARGV[1], ARGV[2], tonumber(ARGV[3])
local found = {}
for i = 4, #ARGV do
  local page = redis.call('ZREVRANGE', base .. ARGV[i], 0, count - 1, 'WITHSCORES')
  for j = 1, #page, 2 do found[#found + 1] = {page[j], tonumber(page[j + 1])} end
end
table.sort(found, function(a, b)
  if a[2] ~= b[2] then return a[2] > b[2] end
  return a[1] > b[1]
end)
local keys = {}
for i = 1, math.min(#found, count) do keys[i] = qprefix .. found[i][1] end
if #keys == 0 then return {} end
local out = {}
for _, doc in ipairs(redis.call('MGET', unpack(keys))) do
  if doc then out[#out + 1] = doc end
end
return out
"""

WIDGET_SLOT = """
-- KEYS[1] throttle, KEYS[2] dirty mark; ARGV: seconds
-- Returns 1 if a push can go now. Else it marks that one more push is necessary when the throttle ends.
if redis.call('SET', KEYS[1], '1', 'NX', 'EX', ARGV[1]) then
  redis.call('DEL', KEYS[2])
  return 1
end
redis.call('SET', KEYS[2], '1', 'EX', ARGV[1] * 4)
return 0
"""

_INBOX = """
-- Removes the items that are past their lifetime: an unread item by created_at, a read item by read_at.
local function prune(p, base, unread_cutoff, read_cutoff)
  for _, part in ipairs({{':unread', unread_cutoff}, {':read', read_cutoff}}) do
    local old = redis.call('ZRANGEBYSCORE', base .. part[1], '-inf', part[2])
    for i = 1, #old, 500 do
      local chunk, docs = {}, {}
      for j = i, math.min(i + 499, #old) do
        chunk[#chunk + 1] = old[j]
        docs[#docs + 1] = p .. ':inb:' .. old[j]
      end
      redis.call('DEL', unpack(docs))
      redis.call('ZREM', base, unpack(chunk))
      redis.call('ZREM', base .. part[1], unpack(chunk))
    end
  end
end
"""

INBOX_ADD = (
    _INBOX
    + """
-- ARGV: prefix, account, key, item, json, created_at, unread cap, unread cutoff, read cutoff
-- Returns -1 if the account does not have the key, else the number of unread items.
local p, aid, kid, id = ARGV[1], ARGV[2], ARGV[3], ARGV[4]
local keyk = p .. ':key:' .. kid
if redis.call('HGET', keyk, 'account_id') ~= aid then return -1 end
local base = keyk .. ':inbox'
local unread = base .. ':unread'
prune(p, base, ARGV[8], ARGV[9])
-- The client sends a write again after a lost connection. The item is then there already.
if redis.call('EXISTS', p .. ':inb:' .. id) == 0 then
  redis.call('SET', p .. ':inb:' .. id, ARGV[5])
  redis.call('ZADD', base, ARGV[6], id)
  redis.call('ZADD', unread, ARGV[6], id)
  local over = redis.call('ZCARD', unread) - tonumber(ARGV[7])
  if over > 0 then
    local oldest = redis.call('ZRANGE', unread, 0, over - 1)
    for _, old in ipairs(oldest) do redis.call('DEL', p .. ':inb:' .. old) end
    redis.call('ZREM', base, unpack(oldest))
    redis.call('ZREM', unread, unpack(oldest))
  end
end
return redis.call('ZCARD', unread)
"""
)

INBOX_TAKE = (
    _INBOX
    + """
-- ARGV: prefix, key, count, mark read ('1' or '0'), now, unread cutoff, read cutoff
-- Returns {unread items after the call, json...} for the oldest unread items.
local p = ARGV[1]
local base = p .. ':key:' .. ARGV[2] .. ':inbox'
local unread = base .. ':unread'
prune(p, base, ARGV[6], ARGV[7])
local ids = redis.call('ZRANGE', unread, 0, tonumber(ARGV[3]) - 1)
local out = {0}
for _, id in ipairs(ids) do
  local doc = redis.call('GET', p .. ':inb:' .. id)
  if doc then out[#out + 1] = doc end
  if ARGV[4] == '1' or not doc then
    redis.call('ZREM', unread, id)
    if doc then redis.call('ZADD', base .. ':read', ARGV[5], id) else redis.call('ZREM', base, id) end
  end
end
out[1] = redis.call('ZCARD', unread)
return out
"""
)

INBOX_VIEW = (
    _INBOX
    + """
-- ARGV: prefix, key, count, unread cutoff, read cutoff
-- Returns {unread items, json, read_at or '', json, read_at or '', ...}, newest first.
local p = ARGV[1]
local base = p .. ':key:' .. ARGV[2] .. ':inbox'
prune(p, base, ARGV[4], ARGV[5])
local out = {redis.call('ZCARD', base .. ':unread')}
for _, id in ipairs(redis.call('ZREVRANGE', base, 0, tonumber(ARGV[3]) - 1)) do
  local doc = redis.call('GET', p .. ':inb:' .. id)
  if doc then
    out[#out + 1] = doc
    out[#out + 1] = redis.call('ZSCORE', base .. ':read', id) or ''
  end
end
return out
"""
)
