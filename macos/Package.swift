// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "Jobtrail",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "Jobtrail",
            path: "Sources/Jobtrail"
        ),
        .testTarget(
            name: "JobtrailTests",
            dependencies: ["Jobtrail"],
            path: "Tests/JobtrailTests"
        ),
    ]
)
