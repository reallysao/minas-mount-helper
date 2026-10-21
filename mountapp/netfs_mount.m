// netfs_mount.m — 用 NetFS 框架挂载/卸载 WebDAV，凭证经私有 stdin 管道（不进 argv）
// 编译: clang -framework Foundation -framework NetFS -o netfs_mount netfs_mount.m
// 协议能力: netfs_mount --protocol  -> 输出 MWDAV_MOUNT_PROTOCOL=2
// 用法:
//   netfs_mount --protocol
//   netfs_mount mount <url> [mountpoint]      # user/pass 经 stdin: "user\npass\n"
//   netfs_mount unmount <mountpoint>
#import <Foundation/Foundation.h>
#import <NetFS/NetFS.h>

#ifndef kNetFSMountFlagsNoBrowse
#define kNetFSMountFlagsNoBrowse 0x00000008
#endif

#define PROTOCOL_MARKER "MWDAV_MOUNT_PROTOCOL=2"

// 从私有 stdin 读取两行：user\npass\n；argv 不含任何凭证。
static BOOL read_credentials(NSString **outUser, NSString **outPass) {
    NSData *data = [[NSFileHandle fileHandleWithStandardInput] readDataToEndOfFile];
    NSString *text = [[NSString alloc] initWithData:data encoding:NSUTF8StringEncoding];
    if (!text) return NO;
    NSArray<NSString *> *lines = [text componentsSeparatedByString:@"\n"];
    if (lines.count < 2) return NO;
    // 协议只移除行分隔符。CRLF 时去掉每行末尾单个 \r；其他空白属于凭证本体。
    NSString *(^stripCR)(NSString *) = ^NSString *(NSString *line) {
        return [line hasSuffix:@"\r"] ? [line substringToIndex:line.length - 1] : line;
    };
    NSString *user = stripCR(lines[0]);
    NSString *pass = stripCR(lines[1]);
    if (user.length == 0) return NO;
    *outUser = user;
    *outPass = pass;
    return YES;
}

static int do_mount(NSString *urlStr, NSString *mountpoint) {
    NSString *user = nil, *pass = nil;
    if (!read_credentials(&user, &pass)) {
        fprintf(stderr, "ERR: 未从 stdin 读取到凭证\n");
        return 3;
    }
    NSURL *serverURL = [NSURL URLWithString:urlStr];
    if (!serverURL) {
        fprintf(stderr, "ERR: bad url\n");
        return 3;
    }
    NSMutableDictionary *openOptions = [NSMutableDictionary dictionary];
    NSMutableDictionary *mountOptions = [NSMutableDictionary dictionary];

    openOptions[(__bridge id)kNetFSUseGuestKey] = @NO;
    if (user.length) {
        openOptions[(__bridge id)kNetFSUserNameKey] = user;
        openOptions[(__bridge id)kNetFSPasswordKey] = pass;
    }
    if (mountpoint.length) {
        [[NSFileManager defaultManager] createDirectoryAtPath:mountpoint
                                 withIntermediateDirectories:YES attributes:nil error:nil];
        mountOptions[(__bridge id)kNetFSMountAtMountDirKey] = @YES;
        mountOptions[(__bridge id)kNetFSAllowLoopbackKey] = @YES;
        mountOptions[(__bridge id)kNetFSMountFlagsKey] = @(kNetFSMountFlagsNoBrowse);
    } else {
        mountOptions[(__bridge id)kNetFSAllowLoopbackKey] = @YES;
    }
    CFURLRef mountPath = NULL;
    if (mountpoint.length) {
        mountPath = (__bridge CFURLRef)[NSURL fileURLWithPath:mountpoint isDirectory:YES];
    }
    CFArrayRef mountpoints = NULL;
    int err = NetFSMountURLSync((__bridge CFURLRef)serverURL, mountPath,
                                (__bridge CFStringRef)(user.length ? user : NULL),
                                (__bridge CFStringRef)(pass.length ? pass : NULL),
                                (__bridge CFMutableDictionaryRef)openOptions,
                                (__bridge CFMutableDictionaryRef)mountOptions,
                                &mountpoints);
    if (mountpoints) CFRelease(mountpoints);
    if (err == 0) {
        printf("MOUNT_OK\n");
        return 0;
    }
    const char *msg = "unknown";
    switch (err) {
        case -1: msg = "generic failure"; break;
        case 2: msg = "no such file"; break;
        case 13: msg = "permission denied"; break;
        case 60: msg = "connection timeout"; break;
        case 65: msg = "no route to host"; break;
        case 68: msg = "not connected"; break;
        case 0x0000FFFF: msg = "authentication failed"; break;
        default: break;
    }
    printf("MOUNT_FAIL err=%d(0x%x) %s\n", err, err, msg);
    return 2;
}

static int do_unmount(NSString *mountpoint) {
    NSTask *task = [[NSTask alloc] init];
    task.launchPath = @"/sbin/umount";
    task.arguments = @[mountpoint];
    NSPipe *pipe = [NSPipe pipe];
    task.standardOutput = pipe;
    task.standardError = pipe;
    [task launch];
    [task waitUntilExit];
    if (task.terminationStatus == 0) {
        printf("UNMOUNT_OK\n");
        return 0;
    }
    NSTask *t2 = [[NSTask alloc] init];
    t2.launchPath = @"/usr/sbin/diskutil";
    t2.arguments = @[@"unmount", mountpoint];
    [t2 launch];
    [t2 waitUntilExit];
    if (t2.terminationStatus == 0) {
        printf("UNMOUNT_OK(via diskutil)\n");
        return 0;
    }
    printf("UNMOUNT_FAIL\n");
    return 2;
}

int main(int argc, char *argv[]) {
    @autoreleasepool {
        if (argc < 2) {
            printf("usage: netfs_mount --protocol\n");
            printf("       netfs_mount mount <url> [mountpoint]\n");
            printf("       netfs_mount unmount <mountpoint>\n");
            return 1;
        }
        NSString *cmd = [NSString stringWithUTF8String:argv[1]];
        if ([cmd isEqualToString:@"--protocol"]) {
            printf("%s\n", PROTOCOL_MARKER);
            return 0;
        }
        if ([cmd isEqualToString:@"mount"]) {
            if (argc < 3) return 1;
            NSString *url = [NSString stringWithUTF8String:argv[2]];
            NSString *mp = (argc >= 4) ? [NSString stringWithUTF8String:argv[3]] : @"";
            return do_mount(url, mp);
        } else if ([cmd isEqualToString:@"unmount"]) {
            if (argc < 3) return 1;
            return do_unmount([NSString stringWithUTF8String:argv[2]]);
        }
    }
    return 1;
}
