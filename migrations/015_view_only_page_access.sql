-- 015: "View only" for a person's page access. With allowed = true and view_only = true the person opens the page
-- and sees everything on it, but cannot save, enter or upload anything through it.
ALTER TABLE user_page_access ADD COLUMN view_only boolean NOT NULL DEFAULT false;
