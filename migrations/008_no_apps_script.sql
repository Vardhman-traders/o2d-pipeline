-- 008: Apps Script and Google Sheets are gone. The O2D screens are served by this app itself (/sales/).
--   * app links may point at this app's own pages (a path like /sales/) as well as https:// addresses
--   * old links to Google-hosted apps are hidden (kept, not deleted, so nothing is lost)
--   * the built-in Sales Portal link is added for the five operating roles
--   * the client-key secret, which only existed to trust the Apps Script caller, is removed

ALTER TABLE app_links DROP CONSTRAINT IF EXISTS app_links_url_check;
ALTER TABLE app_links ADD CONSTRAINT app_links_url_check CHECK (url ~ '^(https://|/[^/])');

UPDATE app_links
SET active = FALSE
WHERE url LIKE '%script.google.com%' OR url LIKE '%googleusercontent.com%';

INSERT INTO app_links (name, url, roles, sort_order, active)
SELECT
    'Sales Portal' AS name,
    '/sales/' AS url,
    ARRAY['shop', 'godown', 'shop_dispatch', 'godown_dispatch', 'receiving'] AS roles,
    0 AS sort_order,
    TRUE AS active
WHERE NOT EXISTS (
    SELECT 1 FROM app_links
    WHERE url = '/sales/'
);

DELETE FROM app_config
WHERE key = 'apps_script_client_key';
