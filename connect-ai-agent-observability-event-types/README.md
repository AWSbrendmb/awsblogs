# Choosing Between TRANSCRIPT_AGENTIC_MESSAGE and TRANSCRIPT_ORCHESTRATION_MESSAGE for Connect AI Agent Observability

Customers building production AI Agents on Amazon Connect need visibility into what's happening inside their conversations — which tools fired, whether authentication succeeded, which knowledge base articles were retrieved, and how often the agent escalates to a human. The data is there, flowing through CloudWatch Logs in real time. But when you open those logs, you find two event types that both seem to contain the tool invocation data you're looking for: `TRANSCRIPT_AGENTIC_MESSAGE` and `TRANSCRIPT_ORCHESTRATION_MESSAGE`.

Which one should you build your analytics pipeline against? The answer depends on whether you're debugging a single conversation or building an operational dashboard — and getting it wrong means either unnecessary parsing complexity or missing the detail you need.

In this post, we explain how these two event types relate to each other, what each contains, when to use which, and how to validate what's available in your specific environment with a five-minute CloudWatch Logs Insights query.

## How Connect AI Agents Generate Log Events

An Amazon Connect AI Agent using orchestration operates in a loop: it receives a customer message, reasons about what to do, optionally calls one or more tools (knowledge base retrieval, authentication, custom Lambda functions), receives results, and generates a response. A single customer utterance can trigger multiple iterations of this loop.

Connect logs this orchestration flow at two levels of granularity simultaneously:

- **Per-step events** (`TRANSCRIPT_ORCHESTRATION_MESSAGE`) — one discrete log record for each atomic step: customer message, bot text, reasoning, tool request, tool result.
- **Per-LLM-invocation snapshots** (`TRANSCRIPT_AGENTIC_MESSAGE`) — one log record per model invocation cycle, containing the complete Bedrock Converse API request (prompt with full conversation history) and the model's response.

Both event types are emitted in parallel for the same conversation. They are complementary views of the same orchestration — not alternatives, not renamed versions of each other.

## Architecture

```
Amazon Connect AI Agent (Orchestration)
│
├──► CloudWatch Logs
│    ├── TRANSCRIPT_ORCHESTRATION_MESSAGE (per-step)
│    │   └── values: [{type: "tool_use", toolUseId, name, arguments}]
│    │   └── values: [{type: "tool_result", toolUseId, name, values, error}]
│    │   └── values: [{type: "text", value: "bot response"}]
│    │   └── values: [{type: "reasoning", value: "internal thought"}]
│    │
│    └── TRANSCRIPT_AGENTIC_MESSAGE (per-LLM-invocation)
│        └── prompt: {full Converse API request — cumulative history}
│        └── completion: {model output — text or tool call}
│
└──► Connect Analytics Data Lake (zero-ETL)
     ├── ai_tool (invocation success, latency, accuracy scores)
     ├── ai_agent_knowledge_base (KB content references)
     ├── ai_session (session-level quality metrics)
     └── ai_prompt (token usage, model info)
```

## Side-by-Side Comparison

| Attribute | TRANSCRIPT_ORCHESTRATION_MESSAGE | TRANSCRIPT_AGENTIC_MESSAGE |
|-----------|--------------------------------|---------------------------|
| **Granularity** | One event per orchestration step | One event per LLM invocation cycle |
| **Key data field** | `values` — typed JSON array (tool_use, tool_result, text, reasoning) | `prompt` (full Converse request) + `completion` (model output) |
| **Conversation history** | Not included — each event is self-contained | Cumulative — every record contains full conversation to that point |
| **Deduplication required?** | No — each tool call is its own event | Yes — later records contain all previous tool calls |
| **Per-tool timestamp?** | Yes — each step has its own `event_timestamp` | No — one timestamp per LLM invocation |
| **Contains system prompt?** | No | Yes — the full system prompt in every record |
| **Contains customer PII?** | Minimal — only in tool arguments/results | Yes — full conversation history including all customer-provided data |
| **Record size** | Small (~5–10 KB) | Large (~50–200 KB, grows with conversation length) |
| **Best for** | Analytics, operational reporting, monitoring, alerting | Debugging, prompt engineering, full-context troubleshooting |

## The Cumulative History Problem

Here's where the architectural difference matters most for pipeline builders. `TRANSCRIPT_AGENTIC_MESSAGE` records accumulate conversation history. In a conversation with 4 tool calls:

- Record 1 contains: tool 1 invocation + result
- Record 2 contains: tools 1–2 invocations + results
- Record 3 contains: tools 1–3 invocations + results
- Record 4 contains: tools 1–4 invocations + results (the terminal record)

If you're building an analytics pipeline on agentic messages, you must either process only the terminal record (identify it by looking for `Complete`, `Escalate`, or `ReturnToContactFlow` in the `completion` field) or deduplicate by `toolUseId` — each tool invocation has a unique ID that appears in all subsequent records.

With `TRANSCRIPT_ORCHESTRATION_MESSAGE`, this problem doesn't exist. Each tool call is its own discrete event.

## When to Use Each

### Use TRANSCRIPT_ORCHESTRATION_MESSAGE when you need to answer operational questions at scale:

- Which tools are being called most frequently?
- What is the success/failure rate for authentication?
- How long does each tool call take?
- Which knowledge base articles are being retrieved?
- Are there conversations where the agent escalated — and why?
- What is the average number of tool calls per conversation?

The typed `values` field makes parsing straightforward — no nested JSON strings to decode, no deduplication across records.

### Use TRANSCRIPT_AGENTIC_MESSAGE when you need full context for a specific conversation:

- Why did the agent choose tool X over tool Y?
- What was the complete prompt context at the point of failure?
- How is my system prompt being interpreted by the model?
- What did the model "see" right before it produced an unexpected response?
- Is the conversation history growing in a way that might hit token limits?

## Validating Your Environment

Before building any pipeline, confirm which event types your log group emits. These CloudWatch Logs Insights queries take approximately five minutes and require zero infrastructure:

**Check for agentic messages:**

```
fields @timestamp, @message.event_type, @message.session_id
| filter @message.event_type = "TRANSCRIPT_AGENTIC_MESSAGE"
| stats count(*) by @message.session_id
| sort @timestamp desc
| limit 20
```

**Check for orchestration messages:**

```
fields @timestamp, @message.event_type
| filter @message.event_type like /ORCHESTRATION/
| stats count(*) by @message.event_type
| limit 100
```

The results determine your parser design:
- **Only agentic messages:** proceed with deduplication by `toolUseId`
- **Both event types:** evaluate orchestration messages as a simpler parsing target
- **Only orchestration messages:** significantly simpler — each tool call is its own discrete event

## Decision Framework

| Your Goal | Recommended Event Type | Why |
|-----------|----------------------|-----|
| Operational dashboard (tool usage, success rates) | ORCHESTRATION_MESSAGE | Typed, per-step, no dedup, small records |
| Alerting (auth failures, escalation spikes) | ORCHESTRATION_MESSAGE | Per-event timestamps enable real-time alerting |
| Cost analytics (token usage per conversation) | AGENTIC_MESSAGE | Contains full prompt — you can count tokens |
| Debugging a specific conversation | AGENTIC_MESSAGE | Full context shows exactly what the model saw |
| Prompt engineering iteration | AGENTIC_MESSAGE | See how your system prompt + conversation history compose |
| Pipeline to reporting database | ORCHESTRATION_MESSAGE | Simpler parsing, lower data volume, less PII exposure |
| Compliance audit trail | Both | Archive agentic for full context; use orchestration for searchable reporting |

## Security Considerations

`TRANSCRIPT_AGENTIC_MESSAGE` records carry your complete system prompt and full conversation history in every record. This includes proprietary orchestration logic, any PII the customer provided during the conversation, knowledge base content retrieved, and tool configurations. If you archive these records to S3, treat that storage with production-database security posture — not log-archive posture.

`TRANSCRIPT_ORCHESTRATION_MESSAGE` records don't carry the system prompt or full history, making them inherently lower-risk for analytics pipelines. Consider stripping the `prompt` field during transformation if you only need tool invocation data from agentic message records.

## Conclusion

Both event types serve important purposes. For most analytics and operational reporting use cases — dashboards, alerting, pipeline-to-database — `TRANSCRIPT_ORCHESTRATION_MESSAGE` is the simpler, safer, and more cost-effective choice. Reserve `TRANSCRIPT_AGENTIC_MESSAGE` for debugging, prompt engineering, and compliance archival where the full LLM invocation context is genuinely needed.

Start by running the Logs Insights queries above to confirm both are available in your environment, then design your pipeline against the event type that matches your use case.

## Related Resources

- [Monitor AI agents using CloudWatch](https://docs.aws.amazon.com/connect/latest/adminguide/monitor-ai-agents.html) — Admin Guide
- [Logging and tracing for Connect AI agents](https://docs.aws.amazon.com/connect/latest/adminguide/viewing-logs-for-connect-ai-agents-self-service.html) — Self-service logging
- [Amazon Connect AI Agents Workshop — CloudWatch Logging](https://catalog.workshops.aws/amazon-connect-ai-agents/en-US/01-foundation/09-logging-observability/05-cloudwatch#core-event-types)
- [AI agent data in the Connect analytics data lake](https://docs.aws.amazon.com/connect/latest/adminguide/data-lake-ai-agent-data.html)

## License

MIT-0
