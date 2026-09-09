// netfs_mount.m — 用 NetFS 框架挂载/卸载 WebDAV，支持用户名密码
// 编译: clang -framework Foundation -framework NetFS -o netfs_mount netfs_mount.m
// 用法:
//   netfs_mount mount <url> <user> <pass> [mountpoint]
//   netfs_mount unmount <mountpoint>
#import <Foundation/Foundation.h>
#import <NetFS/NetFS.h>

#ifndef kNetFSMountFlagsNoBrowse
#define kNetFSMountFlagsNoBrowse 0x00000008
#endif

static int do_mount(NSString *urlStr, NSString *user, NSString *pass, NSString *mountpoint) {
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
    CFURLRef resultURL = NULL;
    int err = NetFSMountURLSync((__bridge CFURLRef)serverURL, mountPath,
                                (__bridge CFStringRef)(user.length ? user : NULL),
                                (__bridge CFStringRef)(pass.length ? pass : NULL),
                                (__bridge CFDictionaryRef)openOptions,
                                (__bridge CFDictionaryRef)mountOptions,
                                &resultURL);
    if (resultURL) CFRelease(resultURL);
    if (err == 0) {
        printf("MOUNT_OK\n");
        return 0;
    }
    // 解析常见错误码
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
    // 兜底: diskutil unmount
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
            printf("usage: netfs_mount mount <url> <user> <pass> [mountpoint]\n");
            printf("       netfs_mount unmount <mountpoint>\n");
            return 1;
        }
        NSString *cmd = [NSString stringWithUTF8String:argv[1]];
        if ([cmd isEqualToString:@"mount"]) {
            if (argc < 5) return 1;
            NSString *url = [NSString stringWithUTF8String:argv[2]];
            NSString *user = [NSString stringWithUTF8String:argv[3]];
            NSString *pass = [NSString stringWithUTF8String:argv[4]];
            NSString *mp = (argc >= 6) ? [NSString stringWithUTF8String:argv[5]] : @"";
            return do_mount(url, user, pass, mp);
        } else if ([cmd isEqualToString:@"unmount"]) {
            if (argc < 3) return 1;
            return do_unmount([NSString stringWithUTF8String:argv[2]]);
        }
    }
    return 1;
}
