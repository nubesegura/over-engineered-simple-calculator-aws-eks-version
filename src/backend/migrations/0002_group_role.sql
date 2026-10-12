-- calc_app_rw: group role that cannot log in; read and write on the calculator tables only.
-- It has no DDL and no role management. The application user (and the clone the rotation
-- function creates) are members of it; the migrate command creates the user.
DO $$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'calc_app_rw') THEN
        CREATE ROLE calc_app_rw NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;

REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO calc_app_rw;
GRANT SELECT, INSERT ON calculations TO calc_app_rw;
