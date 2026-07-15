"""Browser agent server restricted to a single internal website.

The server holds the LLM control loop; a companion browser extension executes
DOM actions and reports results back over an HTTP long-polling connection.
"""
