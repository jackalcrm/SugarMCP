<?php
/**
 * Run SugarCRM's own ModuleScanner against a package directory.
 *
 * Module Loader rejects a package *after* upload with a list of policy violations, which is
 * a slow way to find out. This runs the same scanner the installer uses, against the same
 * instance config, so violations are found before the zip leaves the build.
 *
 * Must run from inside the Sugar host (the scanner needs Sugar's bootstrap):
 *
 *     php tools/scan_package.php /path/to/unzipped/package
 *
 * See tools/scan.sh for the wrapper that does the unzip and invocation.
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
require_once 'ModuleInstall/ModuleScanner.php';

if (empty($argv[1])) {
    fwrite(STDERR, "usage: php scan_package.php <package-directory>\n");
    exit(2);
}

$scanner = new ModuleScanner();
$scanner->scanPackage($argv[1]);
$issues = $scanner->getIssues();

if (empty($issues)) {
    echo "CLEAN — no package-scan issues\n";
    exit(0);
}

echo "ISSUES FOUND:\n";
print_r($issues);
exit(1);
