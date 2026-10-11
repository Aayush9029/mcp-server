---
name: message
description: Send a message to a different agent of the same Relaydeck account. Use when the person asks to tell, ask, or hand off something to another agent, for example "tell Cursor that the API is ready".
---

1. Call the `list_agents` tool of the Relaydeck server. Ignore the entry that has `is_self` set to true.
2. Find the agent that the person named. If no agent matches, or more than one agent matches, ask the person which agent to use.
3. Call `send_message` with `to` and `text`. `to` is the `key_id` of the agent or its exact `label`. Add `task_id` when the message is about one task. The text can have 2,000 characters.
4. Tell the person which agent got the message.

A message to an agent sends no push notification to the person. Use `notify_user` or `ask_user` to reach the person.
