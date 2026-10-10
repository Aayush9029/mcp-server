"""Lua scripts. Each one is one atomic write, so the indexes cannot go out of step.

The scripts build key names from the prefix in ARGV[1]. Thus the store must be one
node (Render Key Value is). Scores stay strings in Lua, because tostring() of a
float loses digits.
"""

_COMMON = """
local p = ARGV[1]
local STATUSES = {'todo', 'in_progress', 'done', 'cancelled'}
local function is_open(s) return s == 'todo' or s == 'in_progress' end
"""

AUTH = """
-- ARGV: prefix, cred hash, now, rate limit, rate bucket, rate ttl, touch interval
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
return {1, cred}
"""

RATE_HIT = """
-- KEYS[1] counter; ARGV: ttl
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
return n
"""

HASH_UPDATE = """
-- KEYS[1] hash; ARGV: guard field, guard value, then field/value pairs
if redis.call('HGET', KEYS[1], ARGV[1]) ~= ARGV[2] then return false end
local pairs_ = {}
for i = 3, #ARGV do pairs_[#pairs_ + 1] = ARGV[i] end
redis.call('HSET', KEYS[1], unpack(pairs_))
return redis.call('HGETALL', KEYS[1])
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
      redis.call('HSET', other, 'apns_token', '', 'push_to_start_token', '')
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
redis.call('DEL', devk)
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
  end
  redis.call('DEL', unpack(tkeys))
  redis.call('ZREM', p .. ':closed', unpack(chunk))
  redis.call('ZREM', acc_tasks, unpack(chunk))
  for _, s in ipairs(STATUSES) do redis.call('ZREM', acc_tasks .. ':' .. s, unpack(chunk)) end
end
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
  end
  redis.call('DEL', unpack(keys))
  redis.call('ZREM', p .. ':closed', unpack(chunk))
end
for _, kid in ipairs(redis.call('ZRANGE', acck .. ':keys', 0, -1)) do
  local keyk = p .. ':key:' .. kid
  local kc = redis.call('HGET', keyk, 'cred')
  if kc then redis.call('DEL', p .. ':cred:' .. kc) end
  redis.call('DEL', keyk, keyk .. ':tasks', p .. ':touch:' .. kid)
  for _, s in ipairs(STATUSES) do redis.call('DEL', keyk .. ':tasks:' .. s) end
end
for _, did in ipairs(redis.call('SMEMBERS', acck .. ':devices')) do
  local devk = p .. ':dev:' .. aid .. ':' .. did
  local token = redis.call('HGET', devk, 'apns_token')
  if token and token ~= '' and redis.call('GET', p .. ':tok:' .. token) == aid .. ':' .. did then
    redis.call('DEL', p .. ':tok:' .. token)
  end
  redis.call('DEL', devk)
end
redis.call('DEL', acck, acck .. ':keys', acck .. ':devices', acc_tasks, p .. ':cred:' .. cred)
for _, s in ipairs(STATUSES) do redis.call('DEL', acc_tasks .. ':' .. s) end
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
if redis.call('HGET', keyk, 'account_id') ~= aid then return 'no_key' end
if is_open(status) and open_count(keyk) >= tonumber(ARGV[8]) then return 'limit' end
redis.call('SET', p .. ':tsk:' .. tid, ARGV[5])
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
-- Returns false on a concurrent write, else the Live Activity pairs that the task had.
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
local lak = p .. ':la:' .. tid
local la = redis.call('HGETALL', lak)
redis.call('DEL', lak)
redis.call('PUBLISH', p .. ':ev:' .. aid, ARGV[7])
return la
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
