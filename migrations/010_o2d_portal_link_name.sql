-- 010: the order-tracker link on the home screen is called "O2D Portal" (only renames the built-in link,
-- and only while it still has the name migration 008 gave it, so a name an admin chose is never overwritten).

UPDATE app_links
SET name = 'O2D Portal'
WHERE url = '/sales/' AND name = 'Sales Portal';
