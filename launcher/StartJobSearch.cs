// StartJobSearch.exe - double-click launcher for the job-search loop.
// 1. Loads private\job-search-config.json (search requirements) and private\.env, and validates them.
// 2. Starts Chrome if it is not running (the browser extension lives there).
// 3. Opens Claude Code in JobSearch\ with a prompt that starts the auto-apply loop.
// Build: launcher\build.cmd  (uses the .NET Framework compiler that ships with Windows)

using System;
using System.Collections;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Web.Script.Serialization;
using System.Windows.Forms;

static class StartJobSearch
{
    const string Title = "Start Job Search";

    [STAThread]
    static int Main()
    {
        try
        {
            Run();
            return 0;
        }
        catch (Exception ex)
        {
            MessageBox.Show(ex.Message, Title, MessageBoxButtons.OK, MessageBoxIcon.Error);
            return 1;
        }
    }

    static void Run()
    {
        // The exe sits in jobSearchAutomation\ (next to JobSearch\, private\, examples\ and claude-browser-bridge\).
        // Real data lives in private\ (gitignored); examples\ holds the public templates.
        string baseDir = AppDomain.CurrentDomain.BaseDirectory;
        string jobDir = FindJobSearchDir(baseDir);
        string privateDir = Path.GetFullPath(Path.Combine(jobDir, "..", "private"));
        string configPath = Path.Combine(privateDir, "job-search-config.json");
        if (!File.Exists(configPath))
            throw new Exception("Config not found:\n" + configPath +
                "\nCopy examples\\job-search-config.example.json to private\\job-search-config.json and fill it in.");

        Dictionary<string, object> config;
        try
        {
            config = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(configPath));
        }
        catch (Exception ex)
        {
            throw new Exception("job-search-config.json is not valid JSON:\n" + ex.Message);
        }

        // ---- validate + summarize the requirements ----
        var roles = StringList(Require(config, "roles"), "roles");
        var locations = ((IEnumerable)Require(config, "locations")).Cast<Dictionary<string, object>>()
            .Select(l => Convert.ToString(Require(l, "name"))).ToList();
        int postedHours = Convert.ToInt32(Require(config, "postedWithinHours"));
        var loop = (Dictionary<string, object>)Require(config, "loop");
        double intervalHours = Convert.ToDouble(Require(loop, "intervalHours"));
        if (roles.Count == 0 || locations.Count == 0)
            throw new Exception("job-search-config.json: \"roles\" and \"locations\" must not be empty.");
        var autoApplyLoop = Require(config, "autoApplyLoop") as Dictionary<string, object>;
        if (autoApplyLoop == null)
            throw new Exception("job-search-config.json: \"autoApplyLoop\" must be an object with a \"steps\" list.");
        var steps = StringList(Require(autoApplyLoop, "steps"), "autoApplyLoop.steps");
        if (steps.Count == 0)
            throw new Exception("job-search-config.json: \"autoApplyLoop.steps\" must not be empty.");
        // ---- .env: private values (${NAME} placeholders) + the ANSWER_* answer bank ----
        string envPath = Path.Combine(privateDir, ".env");
        if (!File.Exists(envPath))
            throw new Exception("Private settings file not found:\n" + envPath + "\nCopy examples\\.env.example to private\\.env and fill it in.");
        var env = LoadEnv(envPath);
        int answerCount = env.Keys.Count(k => k.StartsWith("ANSWER_"));
        if (answerCount == 0)
            throw new Exception(".env has no ANSWER_* entries (the application answer bank):\n" + envPath);
        var missing = System.Text.RegularExpressions.Regex.Matches(File.ReadAllText(configPath), @"\$\{([A-Z0-9_]+)\}")
            .Cast<System.Text.RegularExpressions.Match>().Select(m => m.Groups[1].Value)
            .Where(k => !env.ContainsKey(k) || env[k].Trim() == "").Distinct().ToList();
        if (missing.Count > 0)
            throw new Exception("job-search-config.json uses values that are missing or empty in .env:\n" + string.Join("\n", missing));
        // Load every .env value into this process; Claude Code (and the bridge server it starts) inherit them.
        foreach (var kv in env) Environment.SetEnvironmentVariable(kv.Key, kv.Value);

        var launcher = config.ContainsKey("launcher") ? (Dictionary<string, object>)config["launcher"] : new Dictionary<string, object>();
        string browserTool = Get(launcher, "browserTool", "browser-bridge");
        bool startChrome = Get(launcher, "startChrome", "true").ToLower() == "true";
        string chromePath = Get(launcher, "chromePath", @"C:\Program Files\Google\Chrome\Application\chrome.exe");
        string permissionMode = Get(launcher, "claudePermissionMode", "");

        // --dry-run: print what would be launched instead of launching it.
        bool dryRun = Environment.GetCommandLineArgs().Contains("--dry-run");

        // ---- Chrome ----
        if (!dryRun && startChrome &&Process.GetProcessesByName("chrome").Length == 0)
        {
            if (!File.Exists(chromePath))
                throw new Exception("Chrome not found at:\n" + chromePath + "\nFix launcher.chromePath in job-search-config.json.");
            Process.Start(chromePath);
            System.Threading.Thread.Sleep(3000); // let the extension load
        }

        // ---- Job board page (JobSearch\jobboard.py): the user picks which jobs Claude applies to ----
        var jobBoard = config.ContainsKey("jobBoard") ? (Dictionary<string, object>)config["jobBoard"] : null;
        int boardPort = jobBoard == null ? 0 : Convert.ToInt32(Get(jobBoard, "port", "8766"));
        bool openBoard = jobBoard != null && Get(jobBoard, "openOnStart", "true").ToLower() == "true"
                         && File.Exists(Path.Combine(jobDir, "jobboard.py"));
        string boardUrl = "http://127.0.0.1:" + boardPort + "/";
        string python = openBoard ? FindOnPath("pythonw.exe", "python.exe") : null;
        if (openBoard && python == null)
            throw new Exception("Python not found on PATH - it is needed for the job board (jobBoard.openOnStart).\n" +
                                "Install Python 3 or set jobBoard.openOnStart to false in job-search-config.json.");
        if (openBoard && !dryRun)
        {
            if (!PortOpen(boardPort))
            {
                Process.Start(new ProcessStartInfo(python, "jobboard.py serve --no-browser")
                {
                    WorkingDirectory = jobDir,
                    UseShellExecute = false,
                    CreateNoWindow = true, // runs in the background; stops when Windows signs out
                });
                for (int i = 0; i < 25 && !PortOpen(boardPort); i++) System.Threading.Thread.Sleep(200);
            }
            if (File.Exists(chromePath)) Process.Start(chromePath, boardUrl);
            else Process.Start(boardUrl);
        }

        // ---- Claude Code ----
        string claude = FindClaude();
        string interval = intervalHours == Math.Floor(intervalHours) ? intervalHours + "h" : (int)(intervalHours * 60) + "m";
        string browserText = browserTool == "claude-in-chrome"
            ? "Claude in Chrome (mcp__claude-in-chrome__*)"
            : "the browser-bridge MCP tools (my own Chrome extension)";
        string prompt =
            "Continue my job search (started by StartJobSearch.exe). " +
            "Search requirements and the workflow are in " + configPath + " - read it first " +
            "(roles: " + string.Join(", ", roles) + "; locations: " + string.Join(", ", locations) +
            "; posted within " + postedHours + "h). The application answer bank is the " + answerCount + " ANSWER_* entries in " + envPath +
            " (read that file; it also holds the APPLICANT_* contact values that fill ${NAME} placeholders; they are loaded into this session's environment too). " +
            "Follow CLAUDE.md 'Starting the loop' and the " + steps.Count + " steps in its autoApplyLoop section, with loop interval " + interval +
            ". Use " + browserText + " in Google Chrome for all browsing." +
            (jobBoard == null ? "" :
                " The user picks the jobs to apply to on the job board page " + boardUrl +
                " (autoApplyLoop step 2s): apply only to the jobs listed by python jobboard.py queue, in that order.");

        var args = new List<string>();
        if (permissionMode != "") args.Add("--permission-mode " + Quote(permissionMode));
        args.Add(Quote(prompt));

        if (dryRun)
        {
            Console.WriteLine("cwd:    " + jobDir);
            Console.WriteLine("chrome: " + (startChrome ? chromePath : "(not started)"));
            Console.WriteLine("board:  " + (openBoard ? python + " jobboard.py serve --no-browser -> " + boardUrl +
                                            (PortOpen(boardPort) ? " (already running)" : "") : "(not opened)"));
            Console.WriteLine("env:    " + env.Count + " values loaded from " + envPath + " (" + answerCount + " ANSWER_*)");
            Console.WriteLine("run:    " + claude + " " + string.Join(" ", args));
            return;
        }

        var psi = new ProcessStartInfo(claude, string.Join(" ", args))
        {
            WorkingDirectory = jobDir,
            UseShellExecute = true, // own console window
        };
        Process.Start(psi);
    }

    // Minimal .env reader matching Node's process.loadEnvFile for our file: KEY=value,
    // # comment lines, values optionally wrapped in "...", '...' or `...`.
    static Dictionary<string, string> LoadEnv(string path)
    {
        var env = new Dictionary<string, string>();
        foreach (var raw in File.ReadAllLines(path))
        {
            string line = raw.Trim();
            if (line == "" || line.StartsWith("#") || !line.Contains("=")) continue;
            string key = line.Substring(0, line.IndexOf('=')).Trim();
            string value = line.Substring(line.IndexOf('=') + 1).Trim();
            if (value.Length >= 2 && "\"'`".IndexOf(value[0]) >= 0 && value[value.Length - 1] == value[0])
                value = value.Substring(1, value.Length - 2);
            env[key] = value;
        }
        return env;
    }

    static string FindOnPath(params string[] names)
    {
        foreach (var name in names)
            foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
            {
                try
                {
                    string p = Path.Combine(dir.Trim(), name);
                    // skip the Microsoft Store "python.exe" alias, which only opens the Store
                    if (File.Exists(p) && !p.Contains(@"\Microsoft\WindowsApps\")) return p;
                }
                catch (ArgumentException) { }
            }
        return null;
    }

    static bool PortOpen(int port)
    {
        try
        {
            using (var c = new System.Net.Sockets.TcpClient())
                return c.ConnectAsync("127.0.0.1", port).Wait(300) && c.Connected;
        }
        catch { return false; }
    }

    static string FindJobSearchDir(string baseDir)
    {
        // Allow the exe to live in jobSearchAutomation\ or one level below it (e.g. launcher\).
        foreach (var dir in new[] { baseDir, Path.Combine(baseDir, "..") })
        {
            string candidate = Path.GetFullPath(Path.Combine(dir, "JobSearch"));
            if (Directory.Exists(candidate)) return candidate;
        }
        throw new Exception("Could not find the JobSearch folder next to " + baseDir);
    }

    static string FindClaude()
    {
        string local = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.UserProfile), @".local\bin\claude.exe");
        if (File.Exists(local)) return local;
        foreach (var dir in (Environment.GetEnvironmentVariable("PATH") ?? "").Split(';'))
        {
            foreach (var name in new[] { "claude.exe", "claude.cmd" })
            {
                try
                {
                    string p = Path.Combine(dir.Trim(), name);
                    if (File.Exists(p)) return p;
                }
                catch (ArgumentException) { }
            }
        }
        throw new Exception("Claude Code (claude.exe) not found. Install it or add it to PATH.");
    }

    static object Require(Dictionary<string, object> d, string key)
    {
        object v;
        if (!d.TryGetValue(key, out v) || v == null)
            throw new Exception("job-search-config.json is missing \"" + key + "\".");
        return v;
    }

    static List<string> StringList(object v, string key)
    {
        var list = v as IEnumerable;
        if (list == null || v is string) throw new Exception("job-search-config.json: \"" + key + "\" must be a list.");
        return list.Cast<object>().Select(Convert.ToString).ToList();
    }

    static string Get(Dictionary<string, object> d, string key, string fallback)
    {
        object v;
        return d.TryGetValue(key, out v) && v != null ? Convert.ToString(v) : fallback;
    }

    // Command-line quoting; the prompt never needs embedded double quotes.
    static string Quote(string s)
    {
        return "\"" + s.Replace("\"", "'") + "\"";
    }
}
