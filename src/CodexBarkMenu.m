#import <AppKit/AppKit.h>
#import <mach/mach.h>
#import <math.h>
#import <signal.h>
#import <sys/file.h>
#import <fcntl.h>
#import <IOKit/ps/IOPowerSources.h>
#import <IOKit/ps/IOPSKeys.h>

static NSString *Support(void) {
#ifdef BARK_TEST_SUPPORT_DIR
    return BARK_TEST_SUPPORT_DIR;
#else
    return [NSHomeDirectory() stringByAppendingPathComponent:@"Library/Application Support/CodexBark"];
#endif
}
static NSString *Python(void) {
    NSString *bundled = [Support() stringByAppendingPathComponent:@"python/bin/python3"];
    return [NSFileManager.defaultManager isExecutableFileAtPath:bundled] ? bundled : @"/usr/bin/python3";
}
static NSString *LoginFile(void) { return [NSHomeDirectory() stringByAppendingPathComponent:@"Library/LaunchAgents/local.codex.bark.menu.plist"]; }
static NSUInteger MemoryMB(void) {
    mach_task_basic_info_data_t info; mach_msg_type_number_t count = MACH_TASK_BASIC_INFO_COUNT;
    return task_info(mach_task_self(), MACH_TASK_BASIC_INFO, (task_info_t)&info, &count) == KERN_SUCCESS ? info.resident_size / 1024 / 1024 : 0;
}
static NSDictionary *Backend(NSArray *arguments) {
    NSTask *task = [NSTask new]; task.executableURL = [NSURL fileURLWithPath:Python()];
    task.arguments = [@[[Support() stringByAppendingPathComponent:@"bark.py"]] arrayByAddingObjectsFromArray:arguments];
    NSPipe *pipe = [NSPipe pipe]; task.standardOutput = pipe; task.standardError = NSFileHandle.fileHandleWithNullDevice;
    task.standardInput = NSFileHandle.fileHandleWithNullDevice;
    if (![task launchAndReturnError:nil]) return @{};
    dispatch_source_t timeout = dispatch_source_create(DISPATCH_SOURCE_TYPE_TIMER, 0, 0, dispatch_get_global_queue(QOS_CLASS_UTILITY, 0));
    dispatch_source_set_timer(timeout, dispatch_time(DISPATCH_TIME_NOW, 30 * NSEC_PER_SEC), DISPATCH_TIME_FOREVER, 0);
    dispatch_source_set_event_handler(timeout, ^{ if (task.running) kill(task.processIdentifier, SIGKILL); });
    dispatch_resume(timeout);
    NSData *data = [pipe.fileHandleForReading readDataToEndOfFile]; [task waitUntilExit]; dispatch_source_cancel(timeout);
    if (task.terminationStatus != 0) return @{@"ok": @NO};
    id object = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    if ([object isKindOfClass:NSDictionary.class]) return object;
    NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding] ?: @"";
    return @{@"accepted": @([text containsString:@"服务器已接受"])};
}

static NSDictionary *PowerReading(NSDictionary *d) {
    NSNumber *current = d[@kIOPSCurrentCapacityKey], *maximum = d[@kIOPSMaxCapacityKey];
    if (![current isKindOfClass:NSNumber.class] || ![maximum isKindOfClass:NSNumber.class] || [d[@kIOPSIsPresentKey] isEqual:@NO]) return nil;
    double cap = current.doubleValue, max = maximum.doubleValue;
    if (!isfinite(cap) || !isfinite(max) || max <= 0 || cap < 0 || cap > max) return nil;
    NSString *power = d[@kIOPSPowerSourceStateKey];
    if (![power isEqual:@kIOPSACPowerValue] && ![power isEqual:@kIOPSBatteryPowerValue]) return nil;
    NSInteger percent = (NSInteger)floor(cap * 100.0 / max);
    BOOL onAC = [power isEqual:@kIOPSACPowerValue] || [d[@kIOPSIsChargingKey] boolValue];
    return @{@"percent": @(percent), @"on_ac": @(onAC)};
}

static NSInteger BatteryBucket(NSInteger p) { return p <= 10 ? 0 : (p <= 20 ? 1 : (p <= 25 ? 2 : 3)); }
static BOOL BatteryBoundary(NSInteger prior, BOOL wasAC, NSInteger percent, BOOL onAC) {
    return prior < 0 || wasAC != onAC || BatteryBucket(prior) != BatteryBucket(percent);
}

@interface MenuApp : NSObject <NSApplicationDelegate, NSMenuDelegate>
@property NSStatusItem *status;
@property BOOL enabled;
@property BOOL configured;
@property BOOL quietNow;
@property (strong) NSDictionary *snapshot;
@property (strong) NSDictionary *lastWrittenState;
@property (strong) NSImage *enabledBell;
@property (strong) NSImage *disabledBell;
@property BOOL batteryEnabled;
@property NSInteger lastBatteryPercent;
@property BOOL lastBatteryAC;
@property dispatch_queue_t batteryQueue;
@property (assign) CFRunLoopSourceRef powerSource;
- (void)checkBattery;
@property BOOL busy;
@property BOOL saving;
@property BOOL pendingReload;
@property BOOL menuOpen;
@property NSUInteger generation;
@property dispatch_queue_t backendQueue;
@property NSMenu *contextMenu;
@property NSTimer *resumeTimer;
@property NSTimer *updateTimer;
@property BOOL updateRunning;
@property NSTimeInterval resumeAt;
@end

static void PowerChanged(void *context) { [(__bridge MenuApp *)context checkBattery]; }

@implementation MenuApp
- (void)applicationDidFinishLaunching:(NSNotification *)notification {
    [NSApp setActivationPolicy:NSApplicationActivationPolicyAccessory];
    NSUserDefaults *prefs = NSUserDefaults.standardUserDefaults;
    if ([prefs objectForKey:@"NSStatusItem Preferred Position BarkBridgeStatus"] == nil)
        [prefs setDouble:400 forKey:@"NSStatusItem Preferred Position BarkBridgeStatus"];
    BOOL background = [NSProcessInfo.processInfo.arguments containsObject:@"--background"];
    self.backendQueue = dispatch_queue_create("local.codex.bark.menu.backend", DISPATCH_QUEUE_SERIAL);
    self.lastBatteryPercent = -1;
    self.batteryQueue = dispatch_queue_create("local.codex.bark.power", DISPATCH_QUEUE_SERIAL);
    [NSWorkspace.sharedWorkspace.notificationCenter addObserver:self selector:@selector(powerDidWake:) name:NSWorkspaceDidWakeNotification object:nil];
    [NSDistributedNotificationCenter.defaultCenter addObserver:self selector:@selector(modeChanged:) name:@"CodexBarkModeChanged" object:nil];
    [NSDistributedNotificationCenter.defaultCenter addObserver:self selector:@selector(restore:) name:@"CodexBarkRestoreIcon" object:nil];
    [NSDistributedNotificationCenter.defaultCenter addObserver:self selector:@selector(settingsChanged:) name:@"CodexBarkSettingsChanged" object:nil];
    [NSDistributedNotificationCenter.defaultCenter addObserver:self selector:@selector(updatePreferenceChanged:) name:@"CodexBarkUpdatePreferenceChanged" object:nil];
    BOOL hidden = [prefs boolForKey:@"zeroResidentMode"];
    if (!hidden) [self createStatus];
    self.powerSource = IOPSNotificationCreateRunLoopSource(PowerChanged, (__bridge void *)self);
    if (self.powerSource) CFRunLoopAddSource(CFRunLoopGetMain(), self.powerSource, kCFRunLoopCommonModes);
    [self reload];
    // A short lived updater recovers interrupted transactions before the readiness receipt.
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        NSDictionary *recovery = [self runUpdater:@[@"recover"]];
        if ([recovery[@"recovered"] boolValue]) {
            dispatch_async(dispatch_get_main_queue(), ^{
                NSString *binary = [NSBundle.mainBundle.bundlePath stringByAppendingPathComponent:@"Contents/MacOS/CodexBarkMenu"];
                execl(binary.fileSystemRepresentation, binary.fileSystemRepresentation, "--background", NULL);
                [NSApp terminate:nil];
            });
            return;
        }
        if (![recovery[@"ok"] boolValue]) return;
        NSString *releasePath = [NSBundle.mainBundle pathForResource:@"release" ofType:@"json"];
        NSDictionary *release = [NSJSONSerialization JSONObjectWithData:[NSData dataWithContentsOfFile:releasePath] options:0 error:nil];
        NSDictionary *receipt = @{@"tag": release[@"tag"] ?: @"", @"pid": @(getpid())};
        NSString *directory = [Support() stringByAppendingPathComponent:@"updates"];
        [NSFileManager.defaultManager createDirectoryAtPath:directory withIntermediateDirectories:YES attributes:@{NSFilePosixPermissions:@0700} error:nil];
        NSData *data = [NSJSONSerialization dataWithJSONObject:receipt options:0 error:nil];
        [data writeToFile:[directory stringByAppendingPathComponent:@"ready.json"] atomically:YES];
        [NSFileManager.defaultManager setAttributes:@{NSFilePosixPermissions:@0600} ofItemAtPath:[directory stringByAppendingPathComponent:@"ready.json"] error:nil];
        dispatch_async(dispatch_get_main_queue(), ^{ [self updatePreferenceChanged:nil]; });
    });
    if (!background) [self openPanel:nil];

    dispatch_async(dispatch_get_main_queue(), ^{ [self writeState]; });
}
- (void)powerDidWake:(NSNotification *)notification { self.lastBatteryPercent = -1; [self checkBattery]; [self scheduleUpdateCheck]; }
- (NSDictionary *)runUpdater:(NSArray *)arguments {
    NSTask *task = [NSTask new]; task.executableURL = [NSURL fileURLWithPath:Python()];
    task.arguments = [@[@"-I", @"-B", [Support() stringByAppendingPathComponent:@"updater.py"]] arrayByAddingObjectsFromArray:arguments];
    NSPipe *output = [NSPipe pipe];
    task.standardInput = NSFileHandle.fileHandleWithNullDevice; task.standardOutput = output; task.standardError = NSFileHandle.fileHandleWithNullDevice;
    if (![task launchAndReturnError:nil]) return @{};
    NSData *data = [output.fileHandleForReading readDataToEndOfFile]; [task waitUntilExit];
    id result = [NSJSONSerialization JSONObjectWithData:data options:0 error:nil];
    return [result isKindOfClass:NSDictionary.class] ? result : @{};
}
- (void)updatePreferenceChanged:(NSNotification *)note {
    [self.updateTimer invalidate]; self.updateTimer = nil;
    NSData *data = [NSData dataWithContentsOfFile:[Support() stringByAppendingPathComponent:@"updates/preferences.json"]];
    NSDictionary *prefs = data ? [NSJSONSerialization JSONObjectWithData:data options:0 error:nil] : nil;
    if (![prefs[@"automatic"] isEqual:@YES]) return;
    self.updateTimer = [NSTimer scheduledTimerWithTimeInterval:12*3600 target:self selector:@selector(scheduleUpdateCheck) userInfo:nil repeats:YES];
    self.updateTimer.tolerance = 300;
    [self scheduleUpdateCheck];
}
- (void)scheduleUpdateCheck {
    if (self.updateRunning) return; self.updateRunning = YES;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        [self runUpdater:@[@"check", @"--due", @"--download"]];
        dispatch_async(dispatch_get_main_queue(), ^{ self.updateRunning = NO; });
    });
}
- (void)applicationWillTerminate:(NSNotification *)notification {
    if (self.powerSource) {
        CFRunLoopRemoveSource(CFRunLoopGetMain(), self.powerSource, kCFRunLoopCommonModes);
        CFRunLoopSourceInvalidate(self.powerSource); CFRelease(self.powerSource); self.powerSource = NULL;
    }
}
- (void)checkBattery {
    if (!self.batteryEnabled || !self.powerSource) return;
    CFTypeRef info = IOPSCopyPowerSourcesInfo(); if (!info) return;
    CFArrayRef list = IOPSCopyPowerSourcesList(info);
    NSDictionary *reading = nil;
    if (list) {
        for (CFIndex i = 0; i < CFArrayGetCount(list); i++) {
            NSDictionary *d = (__bridge NSDictionary *)IOPSGetPowerSourceDescription(info, CFArrayGetValueAtIndex(list, i));
            if (![d[@kIOPSTypeKey] isEqual:@kIOPSInternalBatteryType]) continue;
            reading = PowerReading(d);
            if (reading) break;
        }
        CFRelease(list);
    }
    CFRelease(info);
    if (!reading) return;
    NSInteger percent = [reading[@"percent"] integerValue]; BOOL onAC = [reading[@"on_ac"] boolValue];
    NSInteger prior = self.lastBatteryPercent;
    BOOL samePower = onAC == self.lastBatteryAC;
    if (percent == prior && samePower) return;
    BOOL shouldDispatch = BatteryBoundary(prior, self.lastBatteryAC, percent, onAC);
    self.lastBatteryPercent = percent; self.lastBatteryAC = onAC;
    if (!shouldDispatch) return;
    [self writeState];
    dispatch_async(self.batteryQueue, ^{
        NSTask *task = [NSTask new]; task.executableURL = [NSURL fileURLWithPath:Python()];
        task.arguments = @[[Support() stringByAppendingPathComponent:@"bark.py"], @"battery-event"];
        NSPipe *input = [NSPipe pipe]; task.standardInput = input;
        task.standardOutput = NSFileHandle.fileHandleWithNullDevice; task.standardError = NSFileHandle.fileHandleWithNullDevice;
        if ([task launchAndReturnError:nil]) {
            NSData *data = [NSJSONSerialization dataWithJSONObject:reading options:0 error:nil];
            @try { [input.fileHandleForWriting writeData:data]; [input.fileHandleForWriting closeFile]; }
            @catch (NSException *exception) { @try { [input.fileHandleForWriting closeFile]; } @catch (NSException *ignored) { } }
            [task waitUntilExit];
        }
    });
}
- (BOOL)applicationShouldHandleReopen:(NSApplication *)app hasVisibleWindows:(BOOL)visible { [self openPanel:nil]; return YES; }
- (void)createStatus {
    self.status = [NSStatusBar.systemStatusBar statusItemWithLength:NSSquareStatusItemLength];
    self.status.autosaveName = @"BarkBridgeStatus";
    self.status.visible = YES;
    self.status.button.accessibilityLabel = @"Bark 手机推送";
    self.contextMenu = [NSMenu new]; self.contextMenu.delegate = self;
    self.status.menu = nil;
    self.status.button.target = self;
    self.status.button.action = @selector(statusClicked:);
    [self.status.button sendActionOn:NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp];
}
- (void)statusClicked:(id)sender {
    NSEvent *event = NSApp.currentEvent;
    if (event.type == NSEventTypeRightMouseUp) {
        [self toggle:nil];
    } else {
        self.status.menu = self.contextMenu;
        [self.status.button performClick:nil];
        self.status.menu = nil;
        self.status.button.target = self;
        self.status.button.action = @selector(statusClicked:);
        [self.status.button sendActionOn:NSEventMaskLeftMouseUp | NSEventMaskRightMouseUp];
    }
}
- (NSImage *)bell {
    NSImage *cached = self.enabled ? self.enabledBell : self.disabledBell;
    if (cached) return cached;
    NSImage *image = [[NSImage alloc] initWithSize:NSMakeSize(18, 18)]; [image lockFocus];
    [NSColor.blackColor setStroke]; [NSColor.blackColor setFill];
    NSBezierPath *path = [NSBezierPath bezierPath]; path.lineWidth = 1.65;
    [path moveToPoint:NSMakePoint(1, 5.5)];
    [path curveToPoint:NSMakePoint(3, 10) controlPoint1:NSMakePoint(3, 7) controlPoint2:NSMakePoint(3, 8)];
    [path curveToPoint:NSMakePoint(9, 14.5) controlPoint1:NSMakePoint(3, 13) controlPoint2:NSMakePoint(4.5, 14.5)];
    [path curveToPoint:NSMakePoint(15, 10) controlPoint1:NSMakePoint(13.5, 14.5) controlPoint2:NSMakePoint(15, 13)];
    [path curveToPoint:NSMakePoint(17, 5.5) controlPoint1:NSMakePoint(15, 8) controlPoint2:NSMakePoint(15, 7)];
    [path closePath]; [path fill];
    [[NSBezierPath bezierPathWithOvalInRect:NSMakeRect(8.25, 14.0, 1.5, 2.0)] fill];
    [[NSBezierPath bezierPathWithOvalInRect:NSMakeRect(7.2, 2.1, 3.6, 2.2)] fill];
    if (!self.enabled) {
        NSBezierPath *slash = [NSBezierPath bezierPath]; [slash moveToPoint:NSMakePoint(2,16)]; [slash lineToPoint:NSMakePoint(16,2)];
        CGContextRef context = NSGraphicsContext.currentContext.CGContext;
        CGContextSaveGState(context); CGContextSetBlendMode(context, kCGBlendModeClear);
        slash.lineWidth = 4.3; [slash stroke]; CGContextRestoreGState(context);
        slash.lineWidth = 1.7; [NSColor.blackColor setStroke]; [slash stroke];
    }
    [image unlockFocus]; image.template = YES;
    if (self.enabled) self.enabledBell = image; else self.disabledBell = image;
    return image;
}
- (void)reload {
    if (self.saving) { self.pendingReload = YES; return; }
    NSUInteger generation = ++self.generation;
    dispatch_async(self.backendQueue, ^{
        NSDictionary *snapshot = Backend(@[@"snapshot", @"--view", @"menu"]);
        dispatch_async(dispatch_get_main_queue(), ^{
            if (generation != self.generation || self.saving) return;
            if ([snapshot[@"ok"] boolValue]) [self applySnapshot:snapshot];
            else self.status.button.toolTip = @"Bark · 状态暂不可用，请重新打开菜单";
        });
    });
}
- (void)mutate:(NSArray *)arguments {
    if (self.busy || self.saving) return;
    self.saving = YES; self.busy = YES; ++self.generation;
    self.status.button.toolTip = @"Bark · 正在保存…";
    NSArray *request = [arguments arrayByAddingObjectsFromArray:@[@"--with-snapshot", @"--view", @"menu"]];
    dispatch_async(self.backendQueue, ^{
        NSDictionary *result = Backend(request);
        dispatch_async(dispatch_get_main_queue(), ^{
            self.saving = NO; self.busy = NO;
            if ([result[@"ok"] boolValue]) {
                if ([result[@"snapshot"] isKindOfClass:NSDictionary.class] && [result[@"snapshot"][@"ok"] boolValue]) [self applySnapshot:result[@"snapshot"]];
                else [self reload];
                [NSDistributedNotificationCenter.defaultCenter postNotificationName:@"CodexBarkSettingsChanged" object:[@(getpid()) stringValue] userInfo:nil deliverImmediately:YES];
            } else {
                [self reload];
                NSAlert *alert = [NSAlert new]; alert.messageText = @"设置未保存"; alert.informativeText = @"本地操作未完成，请重新打开菜单后重试。";
                [alert addButtonWithTitle:@"好"]; [NSApp activateIgnoringOtherApps:YES]; [alert runModal];
            }
            if (self.pendingReload) { self.pendingReload = NO; [self reload]; }
        });
    });
}
- (void)applySnapshot:(NSDictionary *)snapshot {
    BOOL changed = ![self.snapshot isEqualToDictionary:snapshot];
    self.snapshot = snapshot;
    NSDictionary *d = self.snapshot[@"status"]; self.enabled = [d[@"enabled"] boolValue]; self.configured = [d[@"configured"] boolValue];
    BOOL battery = d[@"battery_enabled"] == nil ? YES : [d[@"battery_enabled"] boolValue];
    if (battery != self.batteryEnabled) self.lastBatteryPercent = -1;
    self.batteryEnabled = battery;
    self.resumeAt = [d[@"resume_at"] doubleValue]; self.quietNow = [d[@"quiet_now"] boolValue];
    [self.resumeTimer invalidate]; self.resumeTimer = nil;
    if (self.resumeAt > NSDate.date.timeIntervalSince1970)
        self.resumeTimer = [NSTimer scheduledTimerWithTimeInterval:MAX(0.1, self.resumeAt - NSDate.date.timeIntervalSince1970) target:self selector:@selector(resumeReached:) userInfo:nil repeats:NO];
    self.status.button.image = [self bell];
    self.status.button.toolTip = [NSString stringWithFormat:@"Bark · %@｜左键菜单，右键开关", self.enabled ? (self.quietNow ? @"每日勿扰中" : @"推送已开启") : @"推送已暂停"];
    [self checkBattery]; [self writeState];
    if (self.menuOpen && changed) { [self buildMenu:self.contextMenu]; [self.contextMenu update]; }
    if ([NSUserDefaults.standardUserDefaults boolForKey:@"zeroResidentMode"] && !self.batteryEnabled) [NSApp terminate:nil];
}
- (void)resumeReached:(NSTimer *)timer {
    [self reload];
    [NSDistributedNotificationCenter.defaultCenter postNotificationName:@"CodexBarkSettingsChanged" object:nil userInfo:nil deliverImmediately:YES];
}
- (void)writeState {
    NSStatusBarButton *button = self.status.button;
    NSRect frame = button.window ? [button.window convertRectToScreen:[button convertRect:button.bounds toView:nil]] : NSZeroRect;
    BOOL onScreen = NO;
    for (NSScreen *screen in NSScreen.screens) if (NSIntersectsRect(screen.frame, frame) && frame.size.width > 0) onScreen = YES;
    NSDictionary *state = @{@"pid": @(getpid()), @"host_view_x": @(frame.origin.x), @"host_view_width": @(frame.size.width), @"host_view_on_screen": @(onScreen), @"helper": @"native_objc", @"enabled": @(self.enabled), @"zero_resident_mode": @([NSUserDefaults.standardUserDefaults boolForKey:@"zeroResidentMode"]), @"battery_monitoring": @(self.batteryEnabled && self.powerSource != NULL), @"battery_percent": @(self.lastBatteryPercent), @"battery_on_ac": @(self.lastBatteryAC)};
    NSString *path = [Support() stringByAppendingPathComponent:@"menu-state.json"];
    if ([self.lastWrittenState isEqualToDictionary:state] && [NSFileManager.defaultManager fileExistsAtPath:path]) return;
    NSMutableDictionary *sample = [state mutableCopy];
    sample[@"memory_mb"] = @(MemoryMB()); sample[@"sampled_at"] = @(NSDate.date.timeIntervalSince1970);
    NSData *data = [NSJSONSerialization dataWithJSONObject:sample options:0 error:nil];
    if ([data writeToFile:path atomically:YES]) {
        [NSFileManager.defaultManager setAttributes:@{NSFilePosixPermissions: @0600} ofItemAtPath:path error:nil];
        self.lastWrittenState = state;
    }
}
- (void)add:(NSMenu *)menu title:(NSString *)title action:(SEL)selector checked:(BOOL)checked {
    NSMenuItem *row = [[NSMenuItem alloc] initWithTitle:title action:selector keyEquivalent:@""];
    row.target = self; row.state = checked ? NSControlStateValueOn : NSControlStateValueOff; [menu addItem:row];
}
- (void)menuNeedsUpdate:(NSMenu *)menu { self.menuOpen = YES; [self reload]; [self buildMenu:menu]; }
- (void)menuDidClose:(NSMenu *)menu { self.menuOpen = NO; }
- (void)buildMenu:(NSMenu *)menu {
    [menu removeAllItems]; menu.autoenablesItems = NO;
    if (self.saving || !self.snapshot) {
        [self add:menu title:self.saving ? @"正在保存…" : @"正在读取状态…" action:NULL checked:NO]; menu.itemArray.lastObject.enabled = NO;
    }
    if (self.enabled && self.quietNow) {
        [self add:menu title:@"每日勿扰中" action:NULL checked:NO];
        menu.itemArray.lastObject.enabled = NO;
        [menu addItem:NSMenuItem.separatorItem];
    }
    if (self.enabled) {
        [self add:menu title:@"暂停推送" action:NULL checked:NO];
        NSMenu *pause = [NSMenu new]; menu.itemArray.lastObject.submenu = pause;
        for (NSArray *option in @[@[@"15 分钟", @900], @[@"1 小时", @3600], @[@"2 小时", @7200]]) {
            [self add:pause title:option[0] action:@selector(pauseTimed:) checked:NO];
            pause.itemArray.lastObject.representedObject = option[1];
        }
        [pause addItem:NSMenuItem.separatorItem];
        [self add:pause title:@"直到手动开启" action:@selector(pauseManual:) checked:NO];
    } else {
        [self add:menu title:@"恢复推送" action:@selector(resume:) checked:NO];
        if (self.resumeAt > 0) {
            NSDateFormatter *format = [NSDateFormatter new]; format.dateFormat = @"HH:mm";
            menu.itemArray.lastObject.toolTip = [@"自动恢复：" stringByAppendingString:[format stringFromDate:[NSDate dateWithTimeIntervalSince1970:self.resumeAt]]];
        }
    }
    [menu addItem:NSMenuItem.separatorItem];
    [self add:menu title:@"提醒场景" action:NULL checked:NO];
    NSMenu *presets = [NSMenu new]; presets.autoenablesItems = NO; menu.itemArray.lastObject.submenu = presets;
    NSString *activePreset = self.snapshot[@"status"][@"preset"];
    for (NSArray *option in @[@[@"需要我时", @"need", @900], @[@"离开电脑", @"away", @900], @[@"专注 15 分钟", @"focus", @900], @[@"专注 1 小时", @"focus", @3600]]) {
        [self add:presets title:option[0] action:@selector(choosePreset:) checked:[activePreset isEqual:option[1]] && ![option[1] isEqual:@"focus"]];
        presets.itemArray.lastObject.representedObject = @{@"preset":option[1], @"seconds":option[2]};
        presets.itemArray.lastObject.enabled = !self.busy;
        presets.itemArray.lastObject.toolTip = @"只调整 Codex 提醒与临时静音，保留其他开关、手机、路由和勿扰。";
    }
    [presets addItem:NSMenuItem.separatorItem];
    [self add:presets title:@"查看预设说明…" action:@selector(openPanel:) checked:NO];
    [self add:menu title:@"通知来源" action:NULL checked:NO];
    NSMenu *sources = [NSMenu new]; menu.itemArray.lastObject.submenu = sources;
    NSArray *entries = self.snapshot[@"sources"];
    if (![entries isKindOfClass:NSArray.class]) entries = @[];
    for (NSDictionary *source in entries) {
        NSString *name = source[@"name"] ?: @"应用";
        NSTimeInterval until = [source[@"mute_until"] doubleValue];
        if ([source[@"enabled"] boolValue] && until > NSDate.date.timeIntervalSince1970) {
            NSDateFormatter *format = [NSDateFormatter new]; format.dateFormat = @"HH:mm";
            name = [name stringByAppendingFormat:@" · 静音至 %@", [format stringFromDate:[NSDate dateWithTimeIntervalSince1970:until]]];
        }
        [self add:sources title:name action:NULL checked:[source[@"enabled"] boolValue]];
        NSMenu *options = [NSMenu new]; options.autoenablesItems = NO; sources.itemArray.lastObject.submenu = options;
        [self add:options title:@"允许推送" action:@selector(toggleSource:) checked:[source[@"enabled"] boolValue]];
        options.itemArray.lastObject.representedObject = source[@"id"]; options.itemArray.lastObject.enabled = !self.busy;
        [options addItem:NSMenuItem.separatorItem];
        for (NSArray *choice in @[@[@"静音 15 分钟", @900], @[@"静音 1 小时", @3600], @[@"结束临时静音", @0]]) {
            [self add:options title:choice[0] action:@selector(muteSource:) checked:NO];
            options.itemArray.lastObject.representedObject = @{@"id": source[@"id"], @"seconds": choice[1]};
            options.itemArray.lastObject.enabled = !self.busy && ([choice[1] integerValue] == 0 || [source[@"enabled"] boolValue]);
        }
    }
    if (entries.count) [sources addItem:NSMenuItem.separatorItem];
    [self add:sources title:@"管理来源…" action:@selector(openSources:) checked:NO];
    [self add:menu title:self.busy ? @"发送中…" : @"发送测试" action:@selector(test:) checked:NO];
    menu.itemArray.lastObject.enabled = self.configured && self.enabled && !self.busy;
    [self add:menu title:@"快捷设置" action:NULL checked:NO];
    NSMenu *quick = [NSMenu new]; menu.itemArray.lastObject.submenu = quick;
    [self add:quick title:@"登录时启动" action:@selector(toggleLogin:) checked:[NSFileManager.defaultManager fileExistsAtPath:LoginFile()]];
    [self add:quick title:@"隐藏菜单栏" action:@selector(zeroMode:) checked:NO];
    [quick addItem:NSMenuItem.separatorItem];
    [self add:quick title:@"重新显示图标" action:@selector(restore:) checked:NO];
    [self add:menu title:@"接收设备" action:NULL checked:NO];
    NSMenu *devices = [NSMenu new]; menu.itemArray.lastObject.submenu = devices;
    menu.itemArray.lastObject.toolTip = @"勾选设备参与接收，可多选。";
    [self buildDeviceMenu:devices devices:self.snapshot[@"devices"]];
    [self add:menu title:@"设置…" action:@selector(openSettings:) checked:NO];
    menu.itemArray.lastObject.keyEquivalent = @",";
    [menu addItem:NSMenuItem.separatorItem];
    [self add:menu title:@"关于 Bark" action:@selector(about:) checked:NO];
    [self add:menu title:@"退出 Bark" action:@selector(quit:) checked:NO];
    menu.itemArray.lastObject.keyEquivalent = @"q";
}
- (void)buildDeviceMenu:(NSMenu *)menu devices:(NSArray *)devices {
    menu.autoenablesItems = NO;
    if (![devices isKindOfClass:NSArray.class]) devices = @[];
    for (NSDictionary *device in devices) {
        NSString *deviceID = device[@"id"];
        if (![deviceID isKindOfClass:NSString.class] || !deviceID.length) continue;
        [self add:menu title:device[@"name"] ?: @"iPhone" action:@selector(toggleDevice:) checked:[device[@"enabled"] boolValue]];
        menu.itemArray.lastObject.representedObject = deviceID;
    }
    if (!menu.numberOfItems) {
        [self add:menu title:@"尚未添加设备" action:NULL checked:NO];
        menu.itemArray.lastObject.enabled = NO;
    }
    [menu addItem:NSMenuItem.separatorItem];
    [self add:menu title:@"管理设备…" action:@selector(openDevices:) checked:NO];
}
- (void)toggleDevice:(NSMenuItem *)sender {
    [self mutate:@[@"device-toggle", @"--device-id", sender.representedObject, @"--device-enabled", @"toggle"]];
}
- (void)openPanel:(id)sender { [self openPanelPage:@"overview"]; }
- (void)openSources:(id)sender { [self openPanelPage:@"sources"]; }
- (void)openSettings:(id)sender { [self openPanelPage:@"settings"]; }
- (void)openDevices:(id)sender { [self openPanelPage:@"devices"]; }
- (void)openPanelPage:(NSString *)page {
    NSURL *url = [NSBundle.mainBundle.bundleURL URLByAppendingPathComponent:@"Contents/Helpers/Codex Bark Panel.app"];
    NSWorkspaceOpenConfiguration *config = NSWorkspaceOpenConfiguration.configuration;
    config.arguments = @[@"--panel-only", [@"--page=" stringByAppendingString:page]]; config.activates = YES;
    [NSWorkspace.sharedWorkspace openApplicationAtURL:url configuration:config completionHandler:^(NSRunningApplication *app, NSError *error) {
        [NSDistributedNotificationCenter.defaultCenter postNotificationName:@"CodexBarkOpenPage" object:nil userInfo:@{@"page":page} deliverImmediately:YES];
        [NSDistributedNotificationCenter.defaultCenter postNotificationName:@"CodexBarkSettingsChanged" object:nil userInfo:nil deliverImmediately:YES];
        if ([NSUserDefaults.standardUserDefaults boolForKey:@"zeroResidentMode"] && !self.batteryEnabled)
            dispatch_async(dispatch_get_main_queue(), ^{ [NSApp terminate:nil]; });
    }];
    [NSDistributedNotificationCenter.defaultCenter postNotificationName:@"CodexBarkOpenPage" object:nil userInfo:@{@"page":page} deliverImmediately:YES];
}
- (void)setPushEnabled:(BOOL)enabled { [self mutate:@[@"set-enabled", enabled ? @"on" : @"off"]]; }
- (void)toggle:(id)sender { [self mutate:@[@"set-enabled", @"toggle"]]; }
- (void)resume:(id)sender { [self setPushEnabled:YES]; }
- (void)pauseManual:(id)sender { [self setPushEnabled:NO]; }
- (void)pauseTimed:(NSMenuItem *)sender { [self mutate:@[@"pause-for", @"--seconds", [sender.representedObject stringValue]]]; }
- (void)toggleSource:(NSMenuItem *)sender { [self mutate:@[@"source-toggle", @"--source-id", sender.representedObject, @"--source-enabled", @"toggle"]]; }
- (void)choosePreset:(NSMenuItem *)sender {
    NSDictionary *data = sender.representedObject;
    NSString *mode = data[@"preset"];
    NSString *changes = [mode isEqual:@"need"] ? @"授权、提问开启；本轮完成关闭；解除 Codex 临时静音。" : ([mode isEqual:@"away"] ? @"授权、提问、本轮完成开启；解除 Codex 临时静音。" : [NSString stringWithFormat:@"仅静音 Codex %@ 分钟，期间跳过且不补发。", [data[@"seconds"] integerValue] == 900 ? @15 : @60]);
    NSAlert *alert = [NSAlert new]; alert.messageText = @"应用提醒场景？";
    alert.informativeText = [changes stringByAppendingString:@"\n总开关、来源停用、手机、路由、勿扰及其他来源继续沿用原设置。"];
    [alert addButtonWithTitle:@"应用预设"]; [alert addButtonWithTitle:@"取消"];
    [NSApp activateIgnoringOtherApps:YES];
    if ([alert runModal] != NSAlertFirstButtonReturn) return;
    [self mutate:@[@"preset-set", @"--preset", data[@"preset"], @"--seconds", [data[@"seconds"] stringValue]]];
}
- (void)muteSource:(NSMenuItem *)sender {
    NSDictionary *data = sender.representedObject;
    [self mutate:@[@"source-mute", @"--source-id", data[@"id"], @"--seconds", [data[@"seconds"] stringValue]]];
}
- (void)about:(id)sender {
    NSAlert *alert = [NSAlert new]; alert.messageText = @"Bark";
    NSString *version = [NSBundle.mainBundle objectForInfoDictionaryKey:@"CFBundleShortVersionString"] ?: @"";
    alert.informativeText = [NSString stringWithFormat:@"版本 %@\n菜单栏内存：%lu MB\n右键铃铛切换推送。", version, (unsigned long)MemoryMB()];
    [alert addButtonWithTitle:@"完成"]; [NSApp activateIgnoringOtherApps:YES]; [alert runModal];
}
- (void)settingsChanged:(NSNotification *)notification {
    if (![notification.object isEqual:[@(getpid()) stringValue]]) [self modeChanged:notification];
}
- (void)test:(id)sender {
    if (self.busy || !self.enabled) return; self.busy = YES;
    dispatch_async(dispatch_get_global_queue(QOS_CLASS_UTILITY, 0), ^{
        NSDictionary *result = Backend(@[@"test-json"]);
        dispatch_async(dispatch_get_main_queue(), ^{
            self.busy = NO; [self reload];
            [NSDistributedNotificationCenter.defaultCenter postNotificationName:@"CodexBarkSettingsChanged" object:nil userInfo:nil deliverImmediately:YES];
            BOOL accepted = [result[@"result"] isEqual:@"accepted"];
            BOOL partial = [result[@"result"] isEqual:@"partial"];
            NSAlert *alert = [NSAlert new]; alert.messageText = accepted ? @"测试已发送" : (partial ? @"部分设备发送成功" : @"发送失败");
            alert.informativeText = accepted ? @"请查看接收设备。" : @"请检查设备开关、地址和网络后重试。";
            [alert addButtonWithTitle:@"好"]; [NSApp activateIgnoringOtherApps:YES]; [alert runModal];
        });
    });
}
- (void)toggleLogin:(id)sender {
    if ([NSFileManager.defaultManager fileExistsAtPath:LoginFile()]) [NSFileManager.defaultManager removeItemAtPath:LoginFile() error:nil];
    else {
        NSDictionary *d = @{@"Label": @"local.codex.bark.menu", @"ProgramArguments": @[@"/usr/bin/open", @"-g", NSBundle.mainBundle.bundlePath, @"--args", @"--background"], @"RunAtLoad": @YES, @"KeepAlive": @NO, @"ProcessType": @"Interactive"};
        NSData *data = [NSPropertyListSerialization dataWithPropertyList:d format:NSPropertyListXMLFormat_v1_0 options:0 error:nil];
        [data writeToFile:LoginFile() atomically:YES];
        [NSFileManager.defaultManager setAttributes:@{NSFilePosixPermissions: @0600} ofItemAtPath:LoginFile() error:nil];
    }
    if ([NSUserDefaults.standardUserDefaults boolForKey:@"zeroResidentMode"]) [NSUserDefaults.standardUserDefaults setBool:[NSFileManager.defaultManager fileExistsAtPath:LoginFile()] forKey:@"loginBeforeZero"];
    [NSDistributedNotificationCenter.defaultCenter postNotificationName:@"CodexBarkSettingsChanged" object:nil userInfo:nil deliverImmediately:YES];
}
- (void)modeChanged:(NSNotification *)notification {
    [NSUserDefaults.standardUserDefaults synchronize];
    if ([NSUserDefaults.standardUserDefaults boolForKey:@"zeroResidentMode"]) {
        if (self.status) { [NSStatusBar.systemStatusBar removeStatusItem:self.status]; self.status = nil; }
    } else if (!self.status) { [self createStatus]; }
    [self reload];
}
- (void)zeroMode:(id)sender {
    [NSUserDefaults.standardUserDefaults setBool:[NSFileManager.defaultManager fileExistsAtPath:LoginFile()] forKey:@"loginBeforeZero"];
    [NSUserDefaults.standardUserDefaults setBool:YES forKey:@"zeroResidentMode"]; [NSUserDefaults.standardUserDefaults synchronize];
    if (!self.batteryEnabled) [NSFileManager.defaultManager removeItemAtPath:LoginFile() error:nil];
    if (self.status) { [NSStatusBar.systemStatusBar removeStatusItem:self.status]; self.status = nil; }
    [self openPanelPage:@"settings"];
    [self writeState];
}
- (void)restore:(id)sender {
    [NSUserDefaults.standardUserDefaults setBool:NO forKey:@"zeroResidentMode"];
    if (self.status) [NSStatusBar.systemStatusBar removeStatusItem:self.status];
    [NSUserDefaults.standardUserDefaults setDouble:400 forKey:@"NSStatusItem Preferred Position BarkBridgeStatus"];
    [NSUserDefaults.standardUserDefaults setBool:YES forKey:@"NSStatusItem Visible BarkBridgeStatus"];
    [self createStatus]; [self reload]; dispatch_async(dispatch_get_main_queue(), ^{ [self writeState]; });
}
- (void)quit:(id)sender { [NSApp terminate:nil]; }
@end

int main(int argc, const char *argv[]) {
    @autoreleasepool {
        if ([NSProcessInfo.processInfo.arguments containsObject:@"--update-smoke-check"]) {
            NSDictionary *result = @{@"version": [NSBundle.mainBundle objectForInfoDictionaryKey:@"CFBundleShortVersionString"] ?: @""};
            NSData *data = [NSJSONSerialization dataWithJSONObject:result options:0 error:nil];
            puts([[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding].UTF8String); return 0;
        }
        NSString *lockPath = [Support() stringByAppendingPathComponent:@".menu-instance.lock"];
        int instanceLock = open(lockPath.fileSystemRepresentation, O_CREAT | O_RDWR | O_CLOEXEC, 0600);
        if (instanceLock < 0 || flock(instanceLock, LOCK_EX | LOCK_NB) != 0) return 0;
        NSApplication *app = NSApplication.sharedApplication; static MenuApp *delegate; delegate = [MenuApp new]; app.delegate = delegate; [app run]; }
    return 0;
}
