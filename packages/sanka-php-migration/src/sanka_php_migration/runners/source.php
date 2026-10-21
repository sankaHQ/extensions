<?php
// SPDX-License-Identifier: Apache-2.0
// Execute the original PHP handlers. No generated IR or target code is loaded here.
ini_set('display_errors', 'stderr');
$settings = json_decode(file_get_contents($argv[1]), true, 512, JSON_THROW_ON_ERROR);
$root = $settings['source'];
$manifest = json_decode(file_get_contents($root . '/sanka-php.json'), true, 512, JSON_THROW_ON_ERROR);
$kernel = null;
if ($manifest['kind'] === 'laravel') {
    require $settings['framework'] . '/vendor/autoload.php';
    $app = Illuminate\Foundation\Application::configure(basePath: $settings['base'])
        ->withRouting(using: function () use ($manifest, $root) {
            foreach ($manifest['route_files'] as $file) { require $root . '/' . $file; }
        })
        ->withExceptions(function ($exceptions) {
            $exceptions->shouldRenderJsonWhen(fn () => true);
            $exceptions->dontReport([Throwable::class]);
        })
        ->create();
    $kernel = $app->make(Illuminate\Contracts\Http\Kernel::class);
    $kernel->bootstrap();
    $app['config']->set('app.debug', false);
    $app['config']->set('app.locale', 'en');
    if ($manifest['database']['dialect'] !== 'none') {
        $url = getenv('DATABASE_URL');
        if ($manifest['database']['dialect'] === 'sqlite') {
            $connection = ['driver'=>'sqlite', 'database'=>substr($url, strlen('sqlite:///')),
                           'prefix'=>'', 'foreign_key_constraints'=>true];
        } else {
            $parsed = parse_url($url);
            $connection = ['driver'=>'mysql', 'host'=>$parsed['host'], 'port'=>$parsed['port'] ?? 3306,
                'database'=>ltrim($parsed['path'], '/'), 'username'=>rawurldecode($parsed['user']),
                'password'=>rawurldecode($parsed['pass'] ?? ''), 'charset'=>'utf8mb4',
                'collation'=>'utf8mb4_unicode_ci', 'prefix'=>'', 'strict'=>true,
                'options'=>[PDO::ATTR_EMULATE_PREPARES=>false, PDO::ATTR_STRINGIFY_FETCHES=>false]];
        }
        $app['config']->set('database.default', 'replay');
        $app['config']->set('database.connections.replay', $connection);
    }
} else {
    $loaded = [];
    foreach ($settings['routes'] as $route) {
        if (!isset($loaded[$route['file']])) {
            require $root . '/' . $route['file'];
            $loaded[$route['file']] = true;
        }
    }
}

while (($line = fgets(STDIN)) !== false) {
    $case = json_decode($line, true, 512, JSON_THROW_ON_ERROR);
    $status = 500;
    $body = ['message'=>'Server Error'];
    $media = 'application/json';
    try {
        if ($kernel !== null) {
            $server = ['HTTP_ACCEPT'=>'application/json', 'CONTENT_TYPE'=>'application/json'];
            foreach ($case['headers'] as $key=>$value) {
                $server['HTTP_' . strtoupper(str_replace('-', '_', $key))] = $value;
            }
            $content = array_key_exists('body', $case) ? json_encode($case['body'], JSON_THROW_ON_ERROR) : null;
            $request = Illuminate\Http\Request::create($case['path'], $case['method'], [], [], [], $server, $content);
            $response = $kernel->handle($request);
            $status = $response->getStatusCode();
            $media = explode(';', $response->headers->get('content-type', ''))[0];
            $encoded = $response->getContent();
            $body = $encoded === '' ? null : json_decode($encoded, false, 512, JSON_THROW_ON_ERROR);
            $kernel->terminate($request, $response);
        } else {
            $path = parse_url($case['path'], PHP_URL_PATH);
            parse_str(parse_url($case['path'], PHP_URL_QUERY) ?? '', $query);
            $matched = false;
            foreach ($settings['routes'] as $route) {
                if ($route['method'] !== $case['method']) { continue; }
                $keys = [];
                $segments = array_map(function ($segment) use (&$keys) {
                    if (preg_match('~^\{([A-Za-z_][A-Za-z0-9_]*)\}$~D', $segment, $item)) {
                        $keys[] = $item[1]; return '([^/]+)';
                    }
                    return preg_quote($segment, '~');
                }, explode('/', $route['path']));
                $pattern = implode('/', $segments);
                if (!preg_match('~^' . $pattern . '$~D', $path, $matches)) { continue; }
                array_shift($matches);
                $params = array_combine($keys, array_map('rawurldecode', $matches));
                $request = ['body'=>$case['body'] ?? [], 'query'=>$query, 'params'=>$params,
                            'headers'=>array_change_key_case($case['headers'], CASE_LOWER)];
                $reply = call_user_func($route['handler'], $request);
                if (!is_array($reply) || count($reply) !== 2 || !isset($reply['status'])
                        || !array_key_exists('body', $reply) || !is_int($reply['status'])) {
                    throw new RuntimeException('invalid response envelope');
                }
                $status = $reply['status']; $body = $reply['body']; $matched = true; break;
            }
            if (!$matched) { $status = 404; $body = ['message'=>'Not Found']; }
        }
    } catch (Throwable $error) {
        $status = 500; $body = ['message'=>'Server Error'];
    }
    // Encode and decode through the real PHP serializer, independently of target compatibility helpers.
    fwrite(STDOUT, json_encode(['status'=>$status, 'media_type'=>$media, 'body'=>$body], JSON_THROW_ON_ERROR) . "\n");
    fflush(STDOUT);
}
