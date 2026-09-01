# Page pings and engagement

**The decision this doc helps you make:** whether an event count includes page pings by mistake, and
how to turn ping counts into an engaged-time figure instead of a page-view figure.

## Page pings are heartbeats, not page views

A page ping (`event_name = 'page_ping'`) is emitted on a fixed interval while the user is active on the
page — it is a heartbeat, not a distinct user action. **Never count page pings as page views.** Filter
them out of event counts by default:

```sql
SELECT *
FROM atomic.events
WHERE event_name != 'page_ping';
```

Any event-count metric that doesn't explicitly exclude `page_ping` will be inflated by however many
heartbeats fired during the session, not just the actions the user actually took.

## Engaged-time pattern

Because each ping represents one heartbeat interval of activity, engaged time can be approximated as:

```
engaged time ≈ number of distinct ping intervals × heartbeat period
```

The heartbeat period is a tracking-configuration value, not a fixed constant — it comes from how the
tracker was configured for this site. Ask the user or check the tracker configuration for the actual
interval rather than assuming a default value.
