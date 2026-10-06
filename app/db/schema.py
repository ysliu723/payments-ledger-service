"""Create the tables, plus the rules the database enforces by itself.

The application checks every rule before it writes. These guards are the
last line of defense: they still hold if a bug slips into the code, or if
someone edits the data from a SQL console.

1. An entry must balance and have at least two lines (checked at commit).
2. Journal lines can never be changed or deleted.
3. Journal entries can never be deleted, and once posted or rejected they
   can never change. A pending entry may only change its status and approval.
"""

from sqlalchemy import Engine

from app.db.models import Base

GUARDS = [
    """
    CREATE OR REPLACE FUNCTION check_entry_balanced() RETURNS trigger AS $$
    DECLARE
        total_debit NUMERIC;
        total_credit NUMERIC;
        line_count INTEGER;
    BEGIN
        SELECT COALESCE(SUM(debit), 0), COALESCE(SUM(credit), 0), COUNT(*)
          INTO total_debit, total_credit, line_count
          FROM journal_lines
         WHERE entry_id = NEW.entry_id;
        IF line_count < 2 OR total_debit <> total_credit THEN
            RAISE EXCEPTION 'journal entry % is not balanced (debits %, credits %, lines %)',
                NEW.entry_id, total_debit, total_credit, line_count;
        END IF;
        RETURN NULL;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS entry_balanced ON journal_lines",
    # DEFERRED: wait until COMMIT, when all the lines of the entry have been inserted.
    """
    CREATE CONSTRAINT TRIGGER entry_balanced
        AFTER INSERT ON journal_lines
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION check_entry_balanced()
    """,
    """
    CREATE OR REPLACE FUNCTION reject_line_change() RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'journal_lines is append-only: % is not allowed', TG_OP;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS lines_append_only ON journal_lines",
    """
    CREATE TRIGGER lines_append_only
        BEFORE UPDATE OR DELETE ON journal_lines
        FOR EACH ROW EXECUTE FUNCTION reject_line_change()
    """,
    """
    CREATE OR REPLACE FUNCTION guard_entry_change() RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'journal_entries is append-only: DELETE is not allowed';
        END IF;
        IF OLD.status <> 'PENDING' THEN
            RAISE EXCEPTION 'journal entry % is %; it can no longer change', OLD.id, OLD.status;
        END IF;
        IF ROW(NEW.posting_date, NEW.description, NEW.source, NEW.currency, NEW.amount,
               NEW.created_by, NEW.created_at, NEW.reverses_entry_id)
           IS DISTINCT FROM
           ROW(OLD.posting_date, OLD.description, OLD.source, OLD.currency, OLD.amount,
               OLD.created_by, OLD.created_at, OLD.reverses_entry_id) THEN
            RAISE EXCEPTION 'journal entry %: only the status and approval of a pending entry may change', OLD.id;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS entries_append_only ON journal_entries",
    """
    CREATE TRIGGER entries_append_only
        BEFORE UPDATE OR DELETE ON journal_entries
        FOR EACH ROW EXECUTE FUNCTION guard_entry_change()
    """,
]


def create_schema(engine: Engine) -> None:
    """Create missing tables, then (re)install the guards. Safe to run more than once."""
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        # Run through psycopg directly: the "%" in RAISE messages would otherwise
        # be taken for a query parameter placeholder.
        psycopg_connection = connection.connection.driver_connection
        for statement in GUARDS:
            psycopg_connection.execute(statement)
