"""The contract between the core and a module."""


class Module:
    """Base class for a dashboard module. Override what the module needs.

    name / title     identifier (used in config.json "modules" and on the page) and display name
    settings         {config key: Setting} this module adds to config.json
    series           {table: [REAL columns]} time-series tables (ts primary key), queried per bucket
    aggregations     {column: "MAX" | "MIN" | "SUM"} how a column combines into a bucket (default AVG)
    schema           other SQL statements run at startup (CREATE TABLE / INDEX IF NOT EXISTS ...)
    retention        DELETE statements run every minute, each taking the cutoff timestamp as "?"
    """

    name = ""
    title = ""
    settings = {}
    series = {}
    aggregations = {}
    schema = []
    retention = []

    def __init__(self, app):
        self.app = app
        self.config = app.config

    def start(self, stop):
        """Start background threads (collectors). `stop` is a threading.Event set on shutdown."""

    def latest(self):
        """Live state merged into /api/latest."""
        return {}

    def metrics(self, conn, since, bucket):
        """Chart data merged into /api/metrics: usually {table: db_query(...)} plus extras."""
        return {}

    def routes(self):
        """Extra GET endpoints: {"/api/x": handler(req)}; handler returns a JSON-able object or bytes."""
        return {}

    def alerts(self, ctx):
        """Active alerts: a list of dicts from dashboard.core.alerts.alert()."""
        return []

    def describe(self):
        """A short fragment for the startup message, e.g. "rpc http://127.0.0.1:3030"."""
        return ""
