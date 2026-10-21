# PHP to Python / FastAPI

Release candidate for explicitly selected custom PHP functions and Laravel route
closures. Scan and Plan are static. Unsupported middleware, dependencies and PHP
semantics block the affected selection. MySQL remains MySQL; SQLite remains SQLite.

Apply requires the reviewed plan hash. Test and Verify run in a prepared disposable
Linux sandbox against the original PHP and parent-observed database effects.
Whole Laravel applications, Sanctum, Blade and CMS migrations are not qualified.

See the shared `sanka-php-migration` package for the exact supported profile.
