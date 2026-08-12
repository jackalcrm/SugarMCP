<?php
/**
 * Run Sugar's Rector-based PHP-compatibility scan against a package.
 *
 * Module Loader runs *two* independent scans. ModuleScanner checks the function/class
 * denylist; a separate Rector pass checks for code patterns that break on newer PHP. A
 * package can pass the first and still be rejected by the second, which is exactly what
 * happened here — so scan.sh runs both.
 */
define('sugarEntry', true);
// Sugar root comes from the environment so this works on any install.
$sugarRoot = getenv('SUGAR_ROOT') ?: '/var/www/html/sugarcrm';
if (!is_dir($sugarRoot . '/include')) {
    fwrite(STDERR, "Not a Sugar install: {$sugarRoot}\n"
        . "Set SUGAR_ROOT to the instance directory.\n");
    exit(2);
}
chdir($sugarRoot);
require_once 'include/entryPoint.php';

use Sugarcrm\Sugarcrm\Rector\RectorScanner;

if (empty($argv[1])) {
    fwrite(STDERR, "usage: php scan_rector.php <package-directory>\n");
    exit(2);
}

$files = [];
$iterator = new RecursiveIteratorIterator(new RecursiveDirectoryIterator($argv[1]));
foreach ($iterator as $file) {
    if ($file->isFile() && strtolower($file->getExtension()) === 'php') {
        $files[] = $file->getPathname();
    }
}

echo 'Scanning ' . count($files) . " PHP file(s)\n";

$scanner = new RectorScanner();
$report = $scanner->scan($files);

if (trim($report) === '') {
    echo "CLEAN — no PHP compatibility issues\n";
    exit(0);
}
echo "PHP COMPATIBILITY ISSUES:\n";
echo $report . "\n";
exit(1);
