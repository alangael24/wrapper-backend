import SwiftUI

struct MascotView: View {
    let color: String
    let shape: BotShape

    var body: some View {
        GeometryReader { proxy in
            ZStack {
                mascotShape
                    .fill(Color(hex: color))
                HStack(spacing: proxy.size.width * 0.13) {
                    Capsule()
                        .fill(.white)
                        .frame(width: proxy.size.width * 0.1, height: proxy.size.height * 0.28)
                        .rotationEffect(.degrees(-8))
                    Capsule()
                        .fill(.white)
                        .frame(width: proxy.size.width * 0.1, height: proxy.size.height * 0.28)
                        .rotationEffect(.degrees(-8))
                }
                .offset(y: -proxy.size.height * 0.05)
            }
        }
        .aspectRatio(1, contentMode: .fit)
        .accessibilityHidden(true)
    }

    private var mascotShape: AnyShape {
        switch shape {
        case .circle: AnyShape(Circle())
        case .bean: AnyShape(BeanShape())
        case .square: AnyShape(RoundedRectangle(cornerRadius: 0.24, style: .continuous))
        case .capsule: AnyShape(Capsule())
        case .triangle: AnyShape(RoundedTriangle())
        case .hexagon: AnyShape(Hexagon())
        case .cloud: AnyShape(CloudShape())
        case .drop: AnyShape(DropShape())
        }
    }
}

private struct BeanShape: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.midX * 0.75, y: rect.minY + rect.height * 0.08))
        path.addCurve(
            to: CGPoint(x: rect.maxX * 0.92, y: rect.midY),
            control1: CGPoint(x: rect.maxX * 0.78, y: rect.minY),
            control2: CGPoint(x: rect.maxX, y: rect.height * 0.2)
        )
        path.addCurve(
            to: CGPoint(x: rect.midX, y: rect.maxY * 0.94),
            control1: CGPoint(x: rect.maxX * 0.9, y: rect.maxY * 0.8),
            control2: CGPoint(x: rect.maxX * 0.72, y: rect.maxY)
        )
        path.addCurve(
            to: CGPoint(x: rect.minX + rect.width * 0.08, y: rect.midY),
            control1: CGPoint(x: rect.width * 0.18, y: rect.maxY),
            control2: CGPoint(x: rect.minX, y: rect.maxY * 0.74)
        )
        path.addCurve(
            to: CGPoint(x: rect.midX * 0.75, y: rect.minY + rect.height * 0.08),
            control1: CGPoint(x: rect.width * 0.08, y: rect.height * 0.2),
            control2: CGPoint(x: rect.width * 0.28, y: rect.minY)
        )
        path.closeSubpath()
        return path
    }
}

private struct RoundedTriangle: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.midX, y: rect.minY + rect.height * 0.06))
        path.addQuadCurve(to: CGPoint(x: rect.maxX * 0.94, y: rect.maxY * 0.86), control: CGPoint(x: rect.maxX, y: rect.maxY))
        path.addQuadCurve(to: CGPoint(x: rect.minX + rect.width * 0.06, y: rect.maxY * 0.86), control: CGPoint(x: rect.midX, y: rect.maxY * 0.96))
        path.addQuadCurve(to: CGPoint(x: rect.midX, y: rect.minY + rect.height * 0.06), control: CGPoint(x: rect.minX, y: rect.maxY))
        return path
    }
}

private struct Hexagon: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        let points = [
            CGPoint(x: rect.midX, y: rect.minY),
            CGPoint(x: rect.maxX, y: rect.height * 0.25),
            CGPoint(x: rect.maxX, y: rect.height * 0.75),
            CGPoint(x: rect.midX, y: rect.maxY),
            CGPoint(x: rect.minX, y: rect.height * 0.75),
            CGPoint(x: rect.minX, y: rect.height * 0.25)
        ]
        path.move(to: points[0])
        points.dropFirst().forEach { path.addLine(to: $0) }
        path.closeSubpath()
        return path
    }
}

private struct CloudShape: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.addEllipse(in: CGRect(x: rect.width * 0.05, y: rect.height * 0.38, width: rect.width * 0.42, height: rect.height * 0.45))
        path.addEllipse(in: CGRect(x: rect.width * 0.27, y: rect.height * 0.15, width: rect.width * 0.48, height: rect.height * 0.62))
        path.addEllipse(in: CGRect(x: rect.width * 0.6, y: rect.height * 0.36, width: rect.width * 0.36, height: rect.height * 0.44))
        path.addRoundedRect(in: CGRect(x: rect.width * 0.18, y: rect.height * 0.48, width: rect.width * 0.66, height: rect.height * 0.4), cornerSize: CGSize(width: rect.width * 0.12, height: rect.height * 0.12))
        return path
    }
}

private struct DropShape: Shape {
    func path(in rect: CGRect) -> Path {
        var path = Path()
        path.move(to: CGPoint(x: rect.midX, y: rect.minY))
        path.addCurve(
            to: CGPoint(x: rect.midX, y: rect.maxY),
            control1: CGPoint(x: rect.maxX * 0.9, y: rect.height * 0.38),
            control2: CGPoint(x: rect.maxX, y: rect.height * 0.68)
        )
        path.addCurve(
            to: CGPoint(x: rect.midX, y: rect.minY),
            control1: CGPoint(x: rect.minX, y: rect.height * 0.68),
            control2: CGPoint(x: rect.width * 0.1, y: rect.height * 0.38)
        )
        return path
    }
}
