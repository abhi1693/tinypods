"""Database lock namespace shared by registry services and the schema invariant."""

# The migration keeps its own historical literal; services take this same lock
# before row locking so an API reparent cannot invert the database trigger order.
TREE_LOCK = 7464699
